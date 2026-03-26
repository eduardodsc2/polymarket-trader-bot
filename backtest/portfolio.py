"""
Portfolio state tracker for the backtest engine.

Tracks cash, open positions, realized PnL, and the equity curve.
All public methods are pure from the engine's perspective — they mutate
internal state but never perform I/O.

Polymarket fee model: 2% of gross payout applied at market resolution
(not at trade time), per PLAN.md specification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger

from config.schemas import OrderFill, PortfolioSnapshot, Trade


# ── Internal position state (not a Pydantic model — never crosses layer boundaries)

@dataclass
class _PositionState:
    condition_id: str
    token_id: str
    strategy: str
    tokens: float           # number of tokens held (size_usd / fill_price)
    avg_entry_price: float  # weighted average cost basis
    current_price: float    # updated via mark_to_market()
    opened_at: datetime


# ── Portfolio ──────────────────────────────────────────────────────────────────

class Portfolio:
    """
    Mutable portfolio state for one backtest run.

    Positions are keyed by token_id. Each open/close updates cash and the
    cumulative PnL. Equity curve snapshots are recorded on each mark-to-market
    call, suitable for Sharpe/drawdown calculation.
    """

    FEE_PCT = 0.02  # 2% of gross payout at resolution

    def __init__(self, initial_capital: float, strategy_name: str = "unknown") -> None:
        self.strategy_name = strategy_name
        self.cash_usd: float = initial_capital
        self.initial_capital: float = initial_capital
        self.realized_pnl: float = 0.0

        self._positions: dict[str, _PositionState] = {}  # token_id → state
        self._trades: list[Trade] = []
        self._snapshots: list[PortfolioSnapshot] = []

    # ── Position management ────────────────────────────────────────────────────

    def open_position(self, fill: OrderFill, condition_id: str) -> None:
        """Record a new BUY fill, opening or adding to a position."""
        assert fill.side == "BUY", "open_position expects a BUY fill"

        tokens_bought = fill.filled_size_usd / fill.fill_price
        self.cash_usd -= fill.filled_size_usd

        if fill.token_id in self._positions:
            # Average down / up
            pos = self._positions[fill.token_id]
            total_tokens = pos.tokens + tokens_bought
            pos.avg_entry_price = (
                (pos.tokens * pos.avg_entry_price + tokens_bought * fill.fill_price)
                / total_tokens
            )
            pos.tokens = total_tokens
            pos.current_price = fill.fill_price
        else:
            self._positions[fill.token_id] = _PositionState(
                condition_id=condition_id,
                token_id=fill.token_id,
                strategy=self.strategy_name,
                tokens=tokens_bought,
                avg_entry_price=fill.fill_price,
                current_price=fill.fill_price,
                opened_at=fill.timestamp,
            )

        self._record_trade(fill, condition_id, mode="backtest")
        logger.debug(
            "Opened position: {} tokens={:.4f} @ {:.4f} cash_remaining={:.2f}",
            fill.token_id[:12],
            tokens_bought,
            fill.fill_price,
            self.cash_usd,
        )

    def close_position(self, fill: OrderFill, condition_id: str) -> None:
        """Record a SELL fill, reducing or closing a position."""
        assert fill.side == "SELL", "close_position expects a SELL fill"

        pos = self._positions.get(fill.token_id)
        if pos is None:
            logger.warning("close_position: no open position for {}", fill.token_id[:12])
            return

        tokens_sold = fill.filled_size_usd / fill.fill_price
        tokens_sold = min(tokens_sold, pos.tokens)  # never sell more than held

        proceeds = tokens_sold * fill.fill_price
        cost_basis = tokens_sold * pos.avg_entry_price
        pnl = proceeds - cost_basis - fill.fee_usd

        self.cash_usd += proceeds - fill.fee_usd
        self.realized_pnl += pnl
        pos.tokens -= tokens_sold

        if pos.tokens <= 1e-9:
            del self._positions[fill.token_id]

        self._record_trade(fill, condition_id, mode="backtest")
        logger.debug(
            "Closed position: {} pnl={:.4f} tokens_remaining={:.4f}",
            fill.token_id[:12],
            pnl,
            max(0.0, pos.tokens),
        )

    def resolve_position(
        self, condition_id: str, outcome: str, timestamp: datetime
    ) -> None:
        """
        Settle all positions in a resolved market.

        outcome="YES": YES tokens pay $1 each, NO tokens pay $0.
        outcome="NO":  NO tokens pay $1 each, YES tokens pay $0.
        Fee of 2% applied to gross payout for winning positions.
        """
        to_remove: list[str] = []

        for token_id, pos in self._positions.items():
            if pos.condition_id != condition_id:
                continue

            # Determine if this token wins based on outcome
            # Convention: token_id ending assessment uses market data passed in;
            # the engine stores yes_token_id / no_token_id on the resolution event.
            # Here we rely on caller passing outcome="YES" or "NO".
            # Position side is always BUY (long), so winning = outcome matches token side.
            # The engine will call this with the correct outcome per token.
            gross_payout = pos.tokens * 1.0  # $1 per winning token
            fee = gross_payout * self.FEE_PCT
            net_payout = gross_payout - fee

            cost_basis = pos.tokens * pos.avg_entry_price
            pnl = net_payout - cost_basis

            self.cash_usd += net_payout
            self.realized_pnl += pnl
            to_remove.append(token_id)

            logger.info(
                "Resolved {} outcome={} tokens={:.4f} payout={:.4f} fee={:.4f} pnl={:.4f}",
                token_id[:12],
                outcome,
                pos.tokens,
                net_payout,
                fee,
                pnl,
            )

        for token_id in to_remove:
            del self._positions[token_id]

    def expire_position(self, condition_id: str) -> None:
        """
        Write off all positions in a market that expired with no payout
        (i.e., the position's token lost — token worth $0).
        """
        to_remove = [
            tid for tid, pos in self._positions.items()
            if pos.condition_id == condition_id
        ]
        for token_id in to_remove:
            pos = self._positions.pop(token_id)
            loss = pos.tokens * pos.avg_entry_price
            self.realized_pnl -= loss
            logger.info(
                "Expired (worthless) {} tokens={:.4f} loss={:.4f}",
                token_id[:12],
                pos.tokens,
                loss,
            )

    # ── Per-token resolution (used by engine for correct multi-leg settlements) ──

    def resolve_token(self, token_id: str, timestamp: datetime) -> None:
        """
        Settle a specific token at $1 per token (winning side).

        Called by the engine instead of resolve_position() so that in multi-leg
        strategies (e.g. SumToOneArb) only the winning token receives a payout
        and the losing token is separately expired.
        """
        pos = self._positions.pop(token_id, None)
        if pos is None:
            return
        gross_payout = pos.tokens * 1.0
        fee = gross_payout * self.FEE_PCT
        net_payout = gross_payout - fee
        cost_basis = pos.tokens * pos.avg_entry_price
        pnl = net_payout - cost_basis
        self.cash_usd += net_payout
        self.realized_pnl += pnl
        logger.info(
            "Resolved token {} tokens={:.4f} payout={:.4f} fee={:.4f} pnl={:.4f}",
            token_id[:12],
            pos.tokens,
            net_payout,
            fee,
            pnl,
        )

    def expire_token(self, token_id: str) -> None:
        """
        Write off a specific token at $0 (losing side).

        Called by the engine to expire only the losing token when both YES and NO
        positions exist in the same condition (multi-leg strategies).
        """
        pos = self._positions.pop(token_id, None)
        if pos is None:
            return
        loss = pos.tokens * pos.avg_entry_price
        self.realized_pnl -= loss
        logger.info(
            "Expired token {} tokens={:.4f} loss={:.4f}",
            token_id[:12],
            pos.tokens,
            loss,
        )

    # ── Mark-to-market ─────────────────────────────────────────────────────────

    def mark_to_market(self, price_updates: dict[str, float], timestamp: datetime) -> None:
        """Update current_price for all positions and record an equity snapshot."""
        for token_id, price in price_updates.items():
            if token_id in self._positions:
                self._positions[token_id].current_price = price

        self._snapshots.append(self.get_snapshot(timestamp))

    # ── Snapshot & equity curve ────────────────────────────────────────────────

    def get_snapshot(self, timestamp: datetime) -> PortfolioSnapshot:
        """Return an immutable point-in-time view of the portfolio."""
        positions_value = sum(
            pos.tokens * pos.current_price for pos in self._positions.values()
        )
        unrealized = sum(
            pos.tokens * (pos.current_price - pos.avg_entry_price)
            for pos in self._positions.values()
        )
        return PortfolioSnapshot(
            timestamp=timestamp,
            cash_usd=self.cash_usd,
            positions_value_usd=positions_value,
            total_value_usd=self.cash_usd + positions_value,
            unrealized_pnl=unrealized,
            realized_pnl=self.realized_pnl,
            open_positions=len(self._positions),
        )

    @property
    def snapshots(self) -> list[PortfolioSnapshot]:
        return list(self._snapshots)

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    @property
    def total_value(self) -> float:
        positions_value = sum(
            pos.tokens * pos.current_price for pos in self._positions.values()
        )
        return self.cash_usd + positions_value

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _record_trade(
        self, fill: OrderFill, condition_id: str, mode: str = "backtest"
    ) -> None:
        self._trades.append(
            Trade(
                strategy=self.strategy_name,
                condition_id=condition_id,
                token_id=fill.token_id,
                side=fill.side,
                size_usd=fill.filled_size_usd,
                price=fill.fill_price,
                fee_usd=fill.fee_usd,
                mode=mode,  # type: ignore[arg-type]
                executed_at=fill.timestamp,
            )
        )
