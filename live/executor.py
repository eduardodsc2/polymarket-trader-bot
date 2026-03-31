"""
Order execution — paper and live modes.

Paper mode:
  - Receives OrderRequest from a strategy (via submit())
  - Validates via RiskManager
  - Simulates fill using FillModel (same logic as backtest)
  - Records trade to in-memory list (DB persistence is Phase 6)
  - Wraps all fill attempts with CircuitBreaker

Live mode (Phase 6):
  - Uses py-clob-client to submit signed orders to Polygon CLOB
  - Same circuit breaker and risk checks as paper mode

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

from backtest.fill_model import FillModel
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
    ) -> None:
        self._settings     = settings
        self._risk_manager = risk_manager
        self._fill_model   = fill_model
        self._on_fill      = on_fill
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


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _run_paper_mode(settings: Settings) -> None:
    """Demo paper trading loop — prints status every 10s until interrupted."""
    risk = RiskManager(settings, initial_capital=settings.initial_capital_usd)
    fill = FillModel(slippage_bps=10)
    executor = PaperExecutor(settings=settings, risk_manager=risk, fill_model=fill)

    logger.info("Paper mode started. Waiting for market data...")
    try:
        while True:
            await asyncio.sleep(10)
            logger.info(
                "Paper executor alive | fills={n} | circuit={state}",
                n=len(executor.trades),
                state=executor.circuit_state.value,
            )
    except asyncio.CancelledError:
        logger.info("Paper executor shutting down.")


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
