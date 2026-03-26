"""
Calibration betting strategy — pre-LLM statistical baseline.

Concept:
  Markets systematically misprice certain event categories. If the historical
  base rate for a market type is 45% but the current price is 70%, the market
  is overpriced → bet NO with positive expected value.

  This strategy does NOT use an LLM. It is the statistical baseline that
  Phase 4's value betting strategy will improve upon using LLM probability
  estimates and news context.

Edge calculation:
    base_rate  = historical resolution rate for this market category
    edge       = base_rate - market_price   (positive → YES underpriced)
    direction  = "YES" if edge > 0, "NO" if edge < 0
    |edge|     > min_edge to trade

Position sizing (fractional Kelly):
    For betting YES at price P with belief base_rate = p:
        payoff b = (1 - P) / P
        Kelly k  = (p * b - (1 - p)) / b
    Multiply by kelly_fraction (default 0.25 = quarter-Kelly).
    Cap at max_position_usdc.

Research questions:
  - Which categories have the largest systematic mispricing?
  - Does the base rate edge persist over time or get arbitraged away?
  - Is the strategy's Brier Score better than a naive 50/50 baseline?
"""
from __future__ import annotations

import uuid

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import Market, OrderRequest, PortfolioSnapshot
from strategies.base_strategy import BaseStrategy


# Historical base rates by market category.
# These are rough priors — a real implementation would compute them from
# resolved market history via data/fetchers/gamma_fetcher.py.
DEFAULT_BASE_RATES: dict[str | None, float] = {
    "crypto":       0.50,   # symmetric — hard to call direction
    "politics":     0.45,   # status-quo bias; incumbents and existing laws tend to hold
    "sports":       0.50,   # symmetric — two competitive sides
    "finance":      0.50,   # symmetric
    "science":      0.55,   # scientific progress / product milestones slightly more likely
    "pop culture":  0.50,   # symmetric
    "news":         0.45,   # sensational events (crashes, scandals) are rarer than priced
    None:           0.50,   # default for unknown category
}


class CalibrationBetting(BaseStrategy):
    """
    Statistical baseline strategy: fade systematically mispriced markets.

    Args:
        market_data:          condition_id → Market.
        base_rates:           Optional category → base_rate override dict.
                              Merged with DEFAULT_BASE_RATES (provided values take precedence).
        min_edge:             Minimum |base_rate - price| required to trade (default 5%).
        kelly_fraction:       Fraction of full Kelly to use (default 0.25 = quarter-Kelly).
        max_position_usdc:    Maximum USD per trade (default $300).
        max_days_to_resolution: Skip markets resolving beyond this horizon (default 90 days).
    """

    name = "calibration_betting"

    def __init__(
        self,
        market_data: dict[str, Market],
        base_rates: dict[str | None, float] | None = None,
        min_edge: float = 0.05,
        kelly_fraction: float = 0.25,
        max_position_usdc: float = 300.0,
        max_days_to_resolution: int = 90,
    ) -> None:
        self.min_edge = min_edge
        self.kelly_fraction = kelly_fraction
        self.max_position_usdc = max_position_usdc
        self.max_days_to_resolution = max_days_to_resolution

        # Merge caller-provided base rates on top of defaults
        self._base_rates: dict[str | None, float] = {**DEFAULT_BASE_RATES}
        if base_rates:
            self._base_rates.update(base_rates)

        # Build lookup tables from market metadata
        self._market_data: dict[str, Market] = market_data
        self._token_to_condition: dict[str, str] = {}
        for cond_id, market in market_data.items():
            if market.yes_token_id:
                self._token_to_condition[market.yes_token_id] = cond_id
            if market.no_token_id:
                self._token_to_condition[market.no_token_id] = cond_id

        # Runtime state — one entry per condition until resolved
        self._entered: set[str] = set()

    # ── Required overrides ──────────────────────────────────────────────────────

    def on_price_update(
        self, event: PriceUpdateEvent, portfolio: PortfolioSnapshot
    ) -> list[OrderRequest]:
        cond_id = self._token_to_condition.get(event.token_id)
        if cond_id is None:
            return []

        # Only trade YES token price updates (one entry per condition)
        market = self._market_data.get(cond_id)
        if market is None or market.yes_token_id != event.token_id:
            return []  # ignore NO token ticks — we act only on YES price

        if cond_id in self._entered:
            return []  # already have a position

        # Check days-to-resolution filter
        if market.end_date is not None:
            remaining = (market.end_date - event.timestamp).total_seconds() / 86_400
            if remaining > self.max_days_to_resolution:
                return []  # market resolves too far in the future

        # Look up base rate for this market's category
        category = (market.category or "").lower() if market.category else None
        base_rate = self._base_rates.get(category, self._base_rates[None])

        # YES price is the market's implied P(YES)
        yes_price = event.price
        edge = base_rate - yes_price  # positive → YES is underpriced

        if abs(edge) <= self.min_edge:
            return []  # not enough edge to justify the trade

        # Determine token to buy based on direction
        if edge > 0:
            # YES underpriced → buy YES
            token_id = market.yes_token_id
            price = yes_price
        else:
            # NO underpriced → buy NO (implied NO price ≈ 1 - yes_price)
            token_id = market.no_token_id
            if token_id is None:
                return []
            price = 1.0 - yes_price  # approximate NO price

        if price <= 0.0:
            return []

        # Fractional Kelly sizing
        size = self._kelly_size(base_rate, price, portfolio, edge)
        if size < 1.0:
            return []

        self._entered.add(cond_id)

        return [
            OrderRequest(
                order_id=str(uuid.uuid4()),
                strategy=self.name,
                condition_id=cond_id,
                token_id=token_id,
                side="BUY",
                size_usd=size,
                limit_price=None,   # market order
                timestamp=event.timestamp,
            )
        ]

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        self._entered.discard(event.condition_id)

    # ── Private helpers ─────────────────────────────────────────────────────────

    def _kelly_size(
        self,
        p: float,
        market_price: float,
        portfolio: PortfolioSnapshot,
        edge: float,
    ) -> float:
        """
        Fractional Kelly position size in USD.

        For a binary bet on outcome with probability p at price market_price:
            payoff b = (1 - market_price) / market_price
            Kelly k  = (p * b - (1 - p)) / b
        """
        if market_price <= 0.0 or market_price >= 1.0:
            return 0.0

        b = (1.0 - market_price) / market_price   # net payoff per $1 risked
        if b <= 0.0:
            return 0.0

        q = 1.0 - p
        kelly = (p * b - q) / b

        if kelly <= 0.0:
            return 0.0

        full_kelly_usd = portfolio.cash_usd * kelly
        fractional_usd = full_kelly_usd * self.kelly_fraction
        return min(fractional_usd, self.max_position_usdc)
