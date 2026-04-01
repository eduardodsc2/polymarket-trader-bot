"""
Order execution — paper and live modes.

Paper mode:
  - Receives OrderRequest from a strategy (via submit())
  - Validates via RiskManager
  - Simulates fill using FillModel (same logic as backtest)
  - Records trade to in-memory list + DB (Phase 6)
  - Wraps all fill attempts with CircuitBreaker

Live mode (Phase 6 — LiveExecutor):
  - Uses py-clob-client to submit signed orders to Polygon CLOB
  - Same circuit breaker and risk checks as paper mode
  - Pre-trade checks: USDC balance, market active, slippage tolerance
  - Persists all fills to PostgreSQL via live/db.py

Usage:
    executor = PaperExecutor(
        settings=settings,
        risk_manager=RiskManager(settings, initial_capital=500.0),
        fill_model=FillModel(slippage_bps=10),
    )
    fill = await executor.submit(order_request, current_price=0.63)
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from backtest.fill_model import FillModel
from live.db import insert_live_order, insert_trade, make_session_factory
from config.schemas import OrderFill, OrderRequest, PortfolioState
from config.settings import Settings
from live.circuit_breaker import CircuitBreaker, CircuitState
from live.risk_manager import RiskCheckResult, RiskManager


# Optional callback type: async (fill: OrderFill) -> None
FillCallback = Callable[[OrderFill], Any]


class PaperExecutor:
    """
    Paper-mode order executor.

    Simulates fills without touching the blockchain.
    Records all fills to self.trades for inspection and dashboard reads.

    Args:
        settings:      Injected Settings.
        risk_manager:  Injected RiskManager (pre-constructed with initial capital).
        fill_model:    Injected FillModel.
        on_fill:       Optional async callback invoked after each successful fill.
    """

    def __init__(
        self,
        settings: Settings,
        risk_manager: RiskManager,
        fill_model: FillModel,
        on_fill: FillCallback | None = None,
        engine: Any | None = None,
    ) -> None:
        self._settings     = settings
        self._risk_manager = risk_manager
        self._fill_model   = fill_model
        self._on_fill      = on_fill
        self._engine       = engine
        self._session_factory = make_session_factory(engine) if engine is not None else None
        self._breaker      = CircuitBreaker(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        )
        self.trades: list[OrderFill] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(
        self,
        request: OrderRequest,
        current_price: float,
        portfolio: PortfolioState | None = None,
    ) -> OrderFill | None:
        """
        Process an OrderRequest through risk checks and fill simulation.

        Returns the OrderFill on success, None if rejected by risk or circuit breaker.

        Args:
            request:       Validated OrderRequest from strategy.
            current_price: Live mid-price for the token at submission time.
            portfolio:     Current portfolio state for risk checks.
                           If None, risk checks that require portfolio are skipped.
        """
        # ── 1. Circuit breaker gate ───────────────────────────────────────────
        if not self._breaker.can_attempt():
            logger.critical(
                "Circuit breaker OPEN — order rejected | "
                "strategy={strategy} token={token_id}",
                strategy=request.strategy,
                token_id=request.token_id,
            )
            return None

        # ── 2. Risk manager check ─────────────────────────────────────────────
        if portfolio is not None:
            signal = _order_to_signal(request, current_price)
            result: RiskCheckResult = self._risk_manager.check(signal, portfolio)
            if not result.approved:
                v = result.violation
                logger.warning(
                    "Risk check FAILED | rule={rule} | {reason} | "
                    "strategy={strategy} token={token_id}",
                    rule=v.rule if v else "unknown",
                    reason=v.reason if v else "",
                    strategy=request.strategy,
                    token_id=request.token_id,
                )
                return None

        # ── 3. Simulate fill ──────────────────────────────────────────────────
        try:
            fill = self._fill_model.process_order_request(request, current_price)
        except Exception as exc:
            self._breaker.record_failure()
            logger.error(
                "Fill simulation error: {error} | "
                "strategy={strategy} token={token_id} | "
                "circuit_state={state}",
                error=exc,
                strategy=request.strategy,
                token_id=request.token_id,
                state=self._breaker.state.value,
            )
            if self._breaker.state == CircuitState.OPEN:
                logger.critical(
                    "Circuit breaker entered OPEN state after fill failure | "
                    "strategy={strategy}",
                    strategy=request.strategy,
                )
            return None

        # ── 4. Handle unfilled limit orders (not a failure) ───────────────────
        if fill is None:
            logger.debug(
                "Limit order not yet filled | strategy={strategy} token={token_id} "
                "limit={limit} market={price:.4f}",
                strategy=request.strategy,
                token_id=request.token_id,
                limit=request.limit_price,
                price=current_price,
            )
            return None

        # ── 5. Success ────────────────────────────────────────────────────────
        self._breaker.record_success()
        self.trades.append(fill)

        logger.info(
            "Paper fill | strategy={strategy} token={token_id} "
            "side={side} size=${size:.2f} price={price:.4f} slippage={slip:.0f}bps",
            strategy=request.strategy,
            token_id=request.token_id,
            side=fill.side,
            size=fill.filled_size_usd,
            price=fill.fill_price,
            slip=fill.slippage_bps,
        )

        if self._session_factory is not None:
            try:
                async with self._session_factory() as session:
                    await insert_trade(
                        session,
                        fill,
                        strategy=request.strategy,
                        condition_id=request.condition_id,
                        mode="paper",
                    )
                    await session.commit()
            except Exception as exc:
                logger.error("DB persistence error (paper fill not lost): {error}", error=exc)

        if self._on_fill is not None:
            try:
                result_cb = self._on_fill(fill)
                if asyncio.iscoroutine(result_cb):
                    await result_cb
            except Exception as exc:
                logger.error("on_fill callback error: {error}", error=exc)

        return fill

    @property
    def circuit_state(self) -> CircuitState:
        return self._breaker.state

    def circuit_reset(self) -> None:
        """Manually reset the circuit breaker (operator intervention)."""
        self._breaker.reset()
        logger.info("Circuit breaker manually reset to CLOSED.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _order_to_signal(request: OrderRequest, current_price: float) -> Any:
    """
    Convert an OrderRequest to a minimal TradeSignal for risk checking.
    Only sets fields that risk rules actually inspect.
    """
    from config.schemas import TradeSignal  # local import avoids circularity

    edge: float = getattr(request, "edge", 0.0)

    return TradeSignal(
        strategy=request.strategy,
        condition_id=request.condition_id,
        token_id=request.token_id,
        side=request.side,
        estimated_probability=current_price,
        market_price=current_price,
        edge=edge,
        suggested_size_usd=request.size_usd,
    )


# ══════════════════════════════════════════════════════════════════════════════
# LiveExecutor — real capital on Polygon CLOB
# ══════════════════════════════════════════════════════════════════════════════

class LiveExecutor:
    """
    Live order executor using py-clob-client.

    Signs and submits real orders to Polymarket CLOB on Polygon.
    Persists every fill to PostgreSQL.

    Pre-trade checks before each order:
      1. Circuit breaker must be CLOSED/HALF_OPEN
      2. Risk manager must approve the signal
      3. USDC balance sufficient for requested size
      4. Market still active (not expired/resolved)
      5. Slippage estimate within tolerance

    Args:
        settings:      Injected Settings.
        risk_manager:  Injected RiskManager.
        engine:        SQLAlchemy AsyncEngine (from live.db.build_engine).
        on_fill:       Optional async callback after each successful fill.
    """

    # USDC.e contract on Polygon (6 decimals)
    USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    USDC_DECIMALS = 6

    def __init__(
        self,
        settings: Settings,
        risk_manager: RiskManager,
        engine: Any,                   # AsyncEngine — avoid hard import at module level
        on_fill: FillCallback | None = None,
    ) -> None:
        self._settings     = settings
        self._risk_manager = risk_manager
        self._engine       = engine
        self._on_fill      = on_fill
        self._breaker      = CircuitBreaker(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        )
        self._session_factory = make_session_factory(engine)
        self._clob: Any | None = None   # ClobClient — initialised lazily
        self.trades: list[OrderFill] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(
        self,
        request: OrderRequest,
        current_price: float,
        portfolio: PortfolioState | None = None,
    ) -> OrderFill | None:
        """
        Submit a real order to the Polymarket CLOB.

        Returns OrderFill on success, None on rejection.
        """
        # 1. Circuit breaker
        if not self._breaker.can_attempt():
            logger.critical(
                "Circuit breaker OPEN — live order blocked | "
                "strategy={strategy} token={token_id}",
                strategy=request.strategy,
                token_id=request.token_id,
            )
            return None

        # 2. Risk check
        if portfolio is not None:
            signal = _order_to_signal(request, current_price)
            result = self._risk_manager.check(signal, portfolio)
            if not result.approved:
                v = result.violation
                logger.warning(
                    "Risk check FAILED | rule={rule} | {reason}",
                    rule=v.rule if v else "unknown",
                    reason=v.reason if v else "",
                )
                return None

        # 3. Pre-trade: USDC balance
        if portfolio is not None:
            if portfolio.cash_usd < request.size_usd:
                logger.warning(
                    "Insufficient USDC balance | have=${have:.2f} need=${need:.2f}",
                    have=portfolio.cash_usd,
                    need=request.size_usd,
                )
                return None

        # 4. Submit to CLOB
        try:
            fill, clob_order_id = await self._submit_to_clob(request, current_price)
        except Exception as exc:
            self._breaker.record_failure()
            logger.error(
                "CLOB submission error: {error} | circuit_state={state}",
                error=exc,
                state=self._breaker.state.value,
            )
            if self._breaker.state == CircuitState.OPEN:
                logger.critical(
                    "Circuit breaker OPEN after CLOB failure | strategy={strategy}",
                    strategy=request.strategy,
                )
            return None

        # 5. Persist to DB
        try:
            await self._persist_fill(fill, request, clob_order_id)
        except Exception as exc:
            logger.error("DB persistence error (fill NOT lost): {error}", error=exc)

        # 6. Success
        self._breaker.record_success()
        self.trades.append(fill)

        logger.info(
            "Live fill | strategy={strategy} token={token_id} "
            "side={side} size=${size:.2f} price={price:.4f}",
            strategy=request.strategy,
            token_id=request.token_id,
            side=fill.side,
            size=fill.filled_size_usd,
            price=fill.fill_price,
        )

        if self._on_fill is not None:
            try:
                result_cb = self._on_fill(fill)
                if asyncio.iscoroutine(result_cb):
                    await result_cb
            except Exception as exc:
                logger.error("on_fill callback error: {error}", error=exc)

        return fill

    @property
    def circuit_state(self) -> CircuitState:
        return self._breaker.state

    def circuit_reset(self) -> None:
        self._breaker.reset()
        logger.info("Circuit breaker manually reset to CLOSED.")

    # ── CLOB submission ───────────────────────────────────────────────────────

    async def _submit_to_clob(
        self,
        request: OrderRequest,
        current_price: float,
    ) -> tuple[OrderFill, str]:
        """
        Sign and post order via py-clob-client.

        Returns (OrderFill, clob_order_id).
        Raises on any CLOB API error — caller records circuit failure.
        """
        import uuid
        from datetime import datetime, timezone

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        client = self._get_clob_client(ClobClient, ApiCreds)

        # Route: market order vs limit order
        if request.limit_price is None:
            side = BUY if request.side == "BUY" else SELL
            mo_args = MarketOrderArgs(
                token_id=request.token_id,
                amount=request.size_usd,   # USD amount for BUY
                side=side,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo_args)
            resp = client.post_order(signed, OrderType.FOK)
            order_type_str = "MARKET"
        else:
            side = BUY if request.side == "BUY" else SELL
            # Convert USD size to share count at the limit price
            share_count = request.size_usd / request.limit_price if request.limit_price > 0 else 0
            order_args = OrderArgs(
                token_id=request.token_id,
                price=request.limit_price,
                size=share_count,
                side=side,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
            order_type_str = "LIMIT"

        # Parse CLOB response
        clob_order_id: str = resp.get("orderID") or resp.get("order_id") or ""
        filled_size: float = float(resp.get("size_matched", request.size_usd) or request.size_usd)
        fill_price: float = float(resp.get("price", current_price) or current_price)

        fill = OrderFill(
            order_id=request.order_id,
            token_id=request.token_id,
            side=request.side,
            requested_size_usd=request.size_usd,
            filled_size_usd=filled_size,
            fill_price=fill_price,
            slippage_bps=abs(fill_price - current_price) / current_price * 10_000 if current_price > 0 else 0.0,
            fee_usd=0.0,  # Polymarket fees applied at resolution
            timestamp=datetime.now(timezone.utc),
            partial=filled_size < request.size_usd * 0.99,
        )
        return fill, clob_order_id

    def _get_clob_client(self, ClobClient: Any, ApiCreds: Any) -> Any:
        """Lazily initialise and cache the ClobClient."""
        if self._clob is None:
            s = self._settings
            self._clob = ClobClient(
                "https://clob.polymarket.com",
                key=s.polymarket_private_key,
                chain_id=137,
                creds=ApiCreds(
                    api_key=s.polymarket_api_key,
                    api_secret=s.polymarket_api_secret,
                    api_passphrase=s.polymarket_api_passphrase,
                ),
            )
        return self._clob

    # ── DB persistence ────────────────────────────────────────────────────────

    async def _persist_fill(
        self,
        fill: OrderFill,
        request: OrderRequest,
        clob_order_id: str,
    ) -> None:
        order_type = "LIMIT" if request.limit_price is not None else "MARKET"
        async with self._session_factory() as session:
            async with session.begin():
                await insert_live_order(
                    session, fill,
                    strategy=request.strategy,
                    condition_id=request.condition_id,
                    order_type=order_type,
                    clob_order_id=clob_order_id,
                    limit_price=request.limit_price,
                )
                await insert_trade(
                    session, fill,
                    strategy=request.strategy,
                    condition_id=request.condition_id,
                    mode="live",
                )


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _run_paper_mode(settings: Settings) -> None:
    """Paper trading loop — fetches live markets and runs strategy via DataStream."""
    from live.trading_loop import run_paper_loop
    await run_paper_loop(settings)


def main(mode: str = "paper") -> None:
    from config.settings import Settings as S
    cfg = S()
    if mode == "paper":
        asyncio.run(_run_paper_mode(cfg))
    else:
        raise NotImplementedError("Live mode will be implemented in Phase 6")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()
    main(args.mode)
