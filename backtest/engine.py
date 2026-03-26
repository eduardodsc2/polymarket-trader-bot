"""
Event-driven backtest engine.

Design:
- Accepts pre-loaded price series and market metadata (no DB calls inside run()).
- Builds a min-heap event queue sorted by (timestamp, sequence_counter).
- Feeds events to the strategy one at a time — no lookahead.
- Applies the fill model to OrderRequests emitted by the strategy.
- Records a portfolio snapshot at each price update for equity curve metrics.

Usage:
    from backtest.engine import BacktestEngine, BacktestResults
    from backtest.fill_model import FillModel

    engine = BacktestEngine(
        strategy=MyStrategy(),
        price_data={"tok_yes": [PricePoint(...), ...]},
        market_data={"cond_123": Market(...)},
        initial_capital=10_000,
        fill_model=FillModel(slippage_bps=10),
        random_seed=42,
    )
    results = engine.run()
"""
from __future__ import annotations

import heapq
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger
from pydantic import BaseModel

from backtest.events import (
    BacktestEvent,
    MarketResolutionEvent,
    OrderFillEvent,
    PriceUpdateEvent,
)
from backtest.fill_model import FillModel
from backtest.portfolio import Portfolio
from config.schemas import (
    BacktestMetrics,
    Market,
    OrderRequest,
    PortfolioSnapshot,
    PricePoint,
    Trade,
)

if TYPE_CHECKING:
    from strategies.base_strategy import BaseStrategy


# ── Heap entry wrapper ─────────────────────────────────────────────────────────

@dataclass(order=True)
class _QueueEntry:
    """Wraps a backtest event with a stable sort key for heapq."""
    timestamp: datetime
    seq: int                                    # tie-breaker — insertion order
    event: BacktestEvent = field(compare=False)


# ── Results container ──────────────────────────────────────────────────────────

class BacktestResults(BaseModel):
    """Full output of a backtest run."""
    metrics: BacktestMetrics
    snapshots: list[PortfolioSnapshot]
    trades: list[Trade]


# ── Engine ─────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Orchestrates a deterministic, event-driven backtest simulation.

    Args:
        strategy:        Strategy instance implementing BaseStrategy.
        price_data:      token_id → list[PricePoint] (sorted ascending by timestamp).
        market_data:     condition_id → Market (needed for resolution events).
        initial_capital: Starting USDC balance.
        fill_model:      FillModel instance (controls slippage).
        random_seed:     Seed applied to Python random + numpy before run().
                         Same seed + same data = byte-identical BacktestMetrics.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        price_data: dict[str, list[PricePoint]],
        market_data: dict[str, Market],
        initial_capital: float = 10_000.0,
        fill_model: FillModel | None = None,
        random_seed: int = 42,
    ) -> None:
        self._strategy = strategy
        self._price_data = price_data
        self._market_data = market_data
        self._initial_capital = initial_capital
        self._fill_model = fill_model or FillModel()
        self._random_seed = random_seed

        # Build token_id → condition_id index for fast lookups
        self._token_to_condition: dict[str, str] = {}
        for cond_id, market in market_data.items():
            if market.yes_token_id:
                self._token_to_condition[market.yes_token_id] = cond_id
            if market.no_token_id:
                self._token_to_condition[market.no_token_id] = cond_id

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self) -> BacktestResults:
        """
        Execute the full backtest.

        1. Seeds Python random and numpy (reproducibility).
        2. Builds the event queue from price_data + market resolution dates.
        3. Processes events in chronological order.
        4. Returns BacktestResults with metrics and full trade/snapshot history.
        """
        random.seed(self._random_seed)
        np.random.seed(self._random_seed)

        portfolio = Portfolio(
            initial_capital=self._initial_capital,
            strategy_name=self._strategy.name,
        )

        # Current known prices per token (for limit order checking)
        current_prices: dict[str, float] = {}
        # Pending limit orders (order_id → OrderRequest)
        pending_limits: dict[str, OrderRequest] = {}

        heap: list[_QueueEntry] = []
        seq = 0

        # ── Build event queue ──────────────────────────────────────────────────

        for token_id, points in self._price_data.items():
            cond_id = self._token_to_condition.get(token_id, "")
            for point in points:
                heapq.heappush(
                    heap,
                    _QueueEntry(
                        timestamp=point.timestamp,
                        seq=seq,
                        event=PriceUpdateEvent(
                            timestamp=point.timestamp,
                            token_id=token_id,
                            price=point.price,
                            condition_id=cond_id,
                        ),
                    ),
                )
                seq += 1

        # Resolution events — fires at market end_date if resolved
        for cond_id, market in self._market_data.items():
            if market.resolved and market.end_date and market.outcome:
                heapq.heappush(
                    heap,
                    _QueueEntry(
                        timestamp=market.end_date,
                        seq=seq,
                        event=MarketResolutionEvent(
                            timestamp=market.end_date,
                            condition_id=cond_id,
                            outcome=market.outcome.upper(),
                            yes_token_id=market.yes_token_id or "",
                            no_token_id=market.no_token_id or "",
                        ),
                    ),
                )
                seq += 1

        logger.info(
            "BacktestEngine: {} events queued, {} markets, initial_capital={:.2f}",
            len(heap),
            len(self._market_data),
            self._initial_capital,
        )

        self._strategy.on_start()

        # ── Event loop ─────────────────────────────────────────────────────────

        while heap:
            entry = heapq.heappop(heap)
            event = entry.event

            if isinstance(event, PriceUpdateEvent):
                current_prices[event.token_id] = event.price

                # Check if any pending limit orders can now fill
                filled_ids: list[str] = []
                for oid, req in pending_limits.items():
                    if req.token_id != event.token_id:
                        continue
                    fill = self._fill_model.process_order_request(req, event.price)
                    if fill is not None:
                        cond_id = self._token_to_condition.get(req.token_id, req.condition_id)
                        portfolio.open_position(fill, cond_id)
                        portfolio.mark_to_market(
                            {fill.token_id: fill.fill_price}, event.timestamp
                        )
                        filled_ids.append(oid)
                for oid in filled_ids:
                    del pending_limits[oid]

                # Update portfolio mark-to-market snapshot
                portfolio.mark_to_market({event.token_id: event.price}, event.timestamp)

                # Ask strategy for orders
                orders: list[OrderRequest] = self._strategy.on_price_update(
                    event, portfolio.get_snapshot(event.timestamp)
                )

                for req in orders:
                    fill = self._fill_model.process_order_request(req, event.price)
                    if fill is None:
                        # Limit order not yet triggered — park it
                        pending_limits[req.order_id] = req
                        continue

                    cond_id = self._token_to_condition.get(req.token_id, req.condition_id)
                    if req.side == "BUY":
                        portfolio.open_position(fill, cond_id)
                    else:
                        portfolio.close_position(fill, cond_id)
                    portfolio.mark_to_market({fill.token_id: fill.fill_price}, event.timestamp)

            elif isinstance(event, MarketResolutionEvent):
                outcome = event.outcome  # "YES" or "NO"
                winning_token = (
                    event.yes_token_id if outcome == "YES" else event.no_token_id
                )
                losing_token = (
                    event.no_token_id if outcome == "YES" else event.yes_token_id
                )

                # Expire pending limit orders for this condition
                for oid in list(pending_limits):
                    req = pending_limits[oid]
                    cond = self._token_to_condition.get(req.token_id, "")
                    if cond == event.condition_id:
                        del pending_limits[oid]

                self._strategy.on_market_resolution(event)

                # Resolve per-token so multi-leg strategies (e.g. SumToOneArb
                # holding both YES and NO) are settled correctly: winning token
                # receives $1 payout; losing token is written off at $0.
                if winning_token:
                    portfolio.resolve_token(winning_token, event.timestamp)
                if losing_token:
                    portfolio.expire_token(losing_token)

                portfolio.mark_to_market({}, event.timestamp)

        self._strategy.on_end()

        # ── Compute metrics ────────────────────────────────────────────────────

        from backtest.metrics import compute_metrics

        snapshots = portfolio.snapshots
        trades = portfolio.trades
        start_date, end_date = _date_range(self._price_data)

        metrics = compute_metrics(
            strategy_name=self._strategy.name,
            snapshots=snapshots,
            trades=trades,
            initial_capital=self._initial_capital,
            start_date=start_date,
            end_date=end_date,
        )

        logger.info(
            "Backtest complete: trades={} final_capital={:.2f} return={:.2f}% sharpe={:.3f}",
            metrics.total_trades,
            metrics.final_capital,
            metrics.total_return_pct * 100,
            metrics.sharpe_ratio,
        )

        return BacktestResults(
            metrics=metrics,
            snapshots=snapshots,
            trades=trades,
        )


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _date_range(
    price_data: dict[str, list[PricePoint]],
) -> tuple[datetime, datetime]:
    """Derive start/end datetimes from the price data."""
    all_ts = [p.timestamp for points in price_data.values() for p in points]
    if not all_ts:
        now = datetime.now(timezone.utc)
        return now, now
    return min(all_ts), max(all_ts)
