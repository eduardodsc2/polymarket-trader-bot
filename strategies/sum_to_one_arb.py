"""
Sum-to-one arbitrage strategy.

Edge: When YES_price + NO_price < 1.00, buying both tokens guarantees a
profit at resolution — one token pays $1 and the other $0. Total cost < $1
per pair → risk-free return equal to the gap.

    edge = 1 - yes_price - no_price
    if edge > min_edge:
        buy YES (market order on YES tick)
        buy NO  (limit order at current NO price — fills on next NO tick)

Position management:
- One entry per condition; no re-entry until the market resolves.
- Per-leg size = min(max_position_usdc, cash / 2).
- Both legs sized equally in USD; token counts differ by price.

Research questions this strategy answers:
- How often does YES + NO < $1 appear historically?
- What is the average edge and time to resolution?
- Is the opportunity clustered in specific market categories?
"""
from __future__ import annotations

import uuid

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import Market, OrderRequest, PortfolioSnapshot
from strategies.base_strategy import BaseStrategy


class SumToOneArb(BaseStrategy):
    """
    Risk-free arbitrage when both YES and NO tokens trade below $1 combined.

    Args:
        market_data:       condition_id → Market (maps tokens to YES/NO sides).
        min_edge:          Minimum guaranteed profit per $1 pair (default 2%).
        max_position_usdc: Maximum USD per leg (default $500).
    """

    name = "sum_to_one_arb"

    def __init__(
        self,
        market_data: dict[str, Market],
        min_edge: float = 0.02,
        max_position_usdc: float = 500.0,
    ) -> None:
        self.min_edge = min_edge
        self.max_position_usdc = max_position_usdc

        # Build YES/NO token lookup tables from market metadata
        self._yes_tokens: dict[str, str] = {}              # condition_id → yes_token_id
        self._no_tokens: dict[str, str] = {}               # condition_id → no_token_id
        self._token_side: dict[str, tuple[str, str]] = {}  # token_id → (condition_id, "YES"|"NO")

        for cond_id, market in market_data.items():
            if market.yes_token_id:
                self._yes_tokens[cond_id] = market.yes_token_id
                self._token_side[market.yes_token_id] = (cond_id, "YES")
            if market.no_token_id:
                self._no_tokens[cond_id] = market.no_token_id
                self._token_side[market.no_token_id] = (cond_id, "NO")

        # Runtime state — cleared on market resolution
        self._last_yes_price: dict[str, float] = {}  # condition_id → price
        self._last_no_price: dict[str, float] = {}   # condition_id → price
        self._entered: set[str] = set()              # condition_ids with active position

    # ── Required overrides ──────────────────────────────────────────────────────

    def on_price_update(
        self, event: PriceUpdateEvent, portfolio: PortfolioSnapshot
    ) -> list[OrderRequest]:
        token_info = self._token_side.get(event.token_id)
        if token_info is None:
            return []

        cond_id, side = token_info

        # Update cached price
        if side == "YES":
            self._last_yes_price[cond_id] = event.price
        else:
            self._last_no_price[cond_id] = event.price

        # Already in position — wait for resolution
        if cond_id in self._entered:
            return []

        yes_price = self._last_yes_price.get(cond_id)
        no_price = self._last_no_price.get(cond_id)
        if yes_price is None or no_price is None:
            return []  # need price for both sides before evaluating

        edge = 1.0 - yes_price - no_price
        if edge <= self.min_edge:
            return []

        total_budget = min(self.max_position_usdc, portfolio.cash_usd)
        if total_budget < 2.0:
            return []

        # Correct sum-to-one arb sizing: buy the SAME NUMBER of tokens on each side.
        # If we buy N tokens of YES and N tokens of NO:
        #   cost = N * yes_price + N * no_price = N * (yes_price + no_price)
        #   payout at resolution = N * $1 (one side wins, other loses)
        #   profit = N - N * (yes_price + no_price) = N * edge  (guaranteed)
        #
        # To achieve equal token counts, split the budget proportionally:
        #   yes_leg_usd / yes_price == no_leg_usd / no_price  →  same token count
        total_price = yes_price + no_price    # < 1.0 because edge > 0
        yes_leg_usd = total_budget * (yes_price / total_price)
        no_leg_usd = total_budget * (no_price / total_price)

        if yes_leg_usd < 0.5 or no_leg_usd < 0.5:
            return []

        # Mark entered before emitting orders (prevents duplicate entry on same tick)
        self._entered.add(cond_id)

        # The token receiving the current price update fills as a market order.
        # The other token is posted as a limit at its last known price — the engine
        # parks it in pending_limits and fills it on the next matching price tick.
        if side == "YES":
            other_token_id = self._no_tokens.get(cond_id)
            other_limit = no_price
            current_leg_usd = yes_leg_usd
            other_leg_usd = no_leg_usd
        else:
            other_token_id = self._yes_tokens.get(cond_id)
            other_limit = yes_price
            current_leg_usd = no_leg_usd
            other_leg_usd = yes_leg_usd

        orders: list[OrderRequest] = [
            OrderRequest(
                order_id=str(uuid.uuid4()),
                strategy=self.name,
                condition_id=cond_id,
                token_id=event.token_id,
                side="BUY",
                size_usd=current_leg_usd,
                limit_price=None,           # market order — fills at current price
                timestamp=event.timestamp,
                edge=edge,
            ),
        ]

        if other_token_id is not None:
            orders.append(
                OrderRequest(
                    order_id=str(uuid.uuid4()),
                    strategy=self.name,
                    condition_id=cond_id,
                    token_id=other_token_id,
                    side="BUY",
                    size_usd=other_leg_usd,
                    limit_price=other_limit,    # limit — fills when price ≤ this
                    timestamp=event.timestamp,
                    edge=edge,
                )
            )

        return orders

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        cond_id = event.condition_id
        self._entered.discard(cond_id)
        self._last_yes_price.pop(cond_id, None)
        self._last_no_price.pop(cond_id, None)
