"""
Abstract base class for all Polymarket trading strategies.

The backtest engine calls on_price_update() and on_market_resolution() on every
event. Strategies return a list of OrderRequest objects — the engine applies the
fill model and updates the portfolio.

Live executor will call the same interface: same strategy code runs in backtest
and live modes with zero modification.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import OrderRequest, PortfolioSnapshot, PortfolioState


class BaseStrategy(ABC):
    """
    All strategies must implement:
      - on_price_update(event, portfolio) → list[OrderRequest]
      - on_market_resolution(event) → None

    Optional hooks:
      - on_start() — called once before first event
      - on_end()   — called once after last event
    """

    name: str = "base"

    # ── Required overrides ─────────────────────────────────────────────────────

    @abstractmethod
    def on_price_update(
        self,
        event: PriceUpdateEvent,
        portfolio: PortfolioSnapshot,
    ) -> list[OrderRequest]:
        """
        Called on every price tick.

        Args:
            event:     Price update with token_id, price, bid, ask, timestamp.
            portfolio: Current portfolio state (read-only snapshot).

        Returns:
            List of OrderRequest objects (may be empty). Market orders have
            limit_price=None; limit orders specify a limit_price.
        """
        ...

    @abstractmethod
    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        """
        Called when a market closes and an outcome is known.

        Strategies should update any internal bookkeeping (e.g., clear cached
        state for the resolved condition_id). The engine handles actual position
        settlement — strategies do not need to issue orders here.
        """
        ...

    # ── Optional lifecycle hooks ───────────────────────────────────────────────

    def on_start(self) -> None:
        """Called once before the first event. Override to initialise state."""

    def on_end(self) -> None:
        """Called once after the last event. Override for cleanup/logging."""

    # ── Helpers available to all strategies ───────────────────────────────────

    def __repr__(self) -> str:
        return f"<Strategy: {self.name}>"
