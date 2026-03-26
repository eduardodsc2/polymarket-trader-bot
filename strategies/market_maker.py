"""
Passive market making / spread capture strategy.

Concept:
  Post limit orders on both sides of the orderbook to earn the bid-ask spread
  from takers who need immediate execution. Adjust quoting when inventory
  becomes skewed to avoid directional exposure.

Backtest design:
  The backtest engine does not support order cancellation or per-order fill
  callbacks. This implementation therefore uses a rolling-mean approach:
  - Maintain a short price history (window ticks) to estimate fair value.
  - Post a limit BUY at fair_value - spread/2 when flat.
  - Post a limit SELL at entry_price + spread when long.
  - Use an internal position tracker to avoid stacking orders.

  This captures the spread-capture concept faithfully while remaining
  compatible with the event-driven engine's constraints.

Research questions:
  - Which market price ranges have the highest fill rate?
  - Does the strategy outperform on markets with high intraday volatility?
  - What is the PnL decomposition: spread income vs. adverse selection loss?
"""
from __future__ import annotations

import uuid

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import Market, OrderRequest, PortfolioSnapshot
from strategies.base_strategy import BaseStrategy


class MarketMaker(BaseStrategy):
    """
    Spread-capture market maker using rolling fair value.

    Args:
        market_data:       condition_id → Market.
        base_spread:       Target bid-ask spread as a fraction of price (default 0.04).
        window:            Number of ticks to use for the rolling fair value (default 5).
        order_size_usdc:   Size of each posted order in USD (default $200).
        max_inventory_pct: Stop posting BUYs when open positions > this fraction of capital.
        min_price:         Do not quote below this price (too close to $0 → huge token counts).
        max_price:         Do not quote above this price (too close to $1 → limited upside).
    """

    name = "market_maker"

    def __init__(
        self,
        market_data: dict[str, Market],
        base_spread: float = 0.04,
        window: int = 5,
        order_size_usdc: float = 200.0,
        max_inventory_pct: float = 0.40,
        min_price: float = 0.05,
        max_price: float = 0.95,
    ) -> None:
        self.base_spread = base_spread
        self.window = window
        self.order_size_usdc = order_size_usdc
        self.max_inventory_pct = max_inventory_pct
        self.min_price = min_price
        self.max_price = max_price

        # Build token → condition_id index
        self._token_to_condition: dict[str, str] = {}
        for cond_id, market in market_data.items():
            if market.yes_token_id:
                self._token_to_condition[market.yes_token_id] = cond_id
            if market.no_token_id:
                self._token_to_condition[market.no_token_id] = cond_id

        # Per-condition runtime state
        self._price_history: dict[str, list[float]] = {}  # condition_id → recent prices
        self._in_position: set[str] = set()               # conditions where we hold tokens
        self._entry_price: dict[str, float] = {}          # condition_id → buy entry price

    # ── Required overrides ──────────────────────────────────────────────────────

    def on_price_update(
        self, event: PriceUpdateEvent, portfolio: PortfolioSnapshot
    ) -> list[OrderRequest]:
        cond_id = self._token_to_condition.get(event.token_id)
        if cond_id is None:
            return []

        price = event.price
        if price < self.min_price or price > self.max_price:
            return []  # too close to resolution boundary — don't quote

        # Update rolling price history
        hist = self._price_history.setdefault(cond_id, [])
        hist.append(price)
        if len(hist) > self.window:
            hist.pop(0)

        if len(hist) < self.window:
            return []  # not enough data to estimate fair value

        fair_value = sum(hist) / len(hist)
        buy_threshold = fair_value - self.base_spread / 2.0
        orders: list[OrderRequest] = []

        if cond_id in self._in_position:
            # ── Sell side: exit when price recovers above entry + full spread ──
            entry = self._entry_price[cond_id]
            sell_target = entry + self.base_spread
            if price >= sell_target:
                orders.append(
                    OrderRequest(
                        order_id=str(uuid.uuid4()),
                        strategy=self.name,
                        condition_id=cond_id,
                        token_id=event.token_id,
                        side="SELL",
                        size_usd=self.order_size_usdc,
                        limit_price=None,   # market sell — take the profit now
                        timestamp=event.timestamp,
                    )
                )
                self._in_position.discard(cond_id)
                self._entry_price.pop(cond_id, None)
        else:
            # ── Buy side: enter when price drops to buy threshold ──────────────
            max_pos_usd = portfolio.total_value_usd * self.max_inventory_pct
            if (
                price <= buy_threshold
                and portfolio.positions_value_usd < max_pos_usd
                and portfolio.cash_usd >= self.order_size_usdc
            ):
                orders.append(
                    OrderRequest(
                        order_id=str(uuid.uuid4()),
                        strategy=self.name,
                        condition_id=cond_id,
                        token_id=event.token_id,
                        side="BUY",
                        size_usd=self.order_size_usdc,
                        limit_price=None,   # market buy
                        timestamp=event.timestamp,
                    )
                )
                self._in_position.add(cond_id)
                self._entry_price[cond_id] = price

        return orders

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        cond_id = event.condition_id
        self._price_history.pop(cond_id, None)
        self._in_position.discard(cond_id)
        self._entry_price.pop(cond_id, None)
