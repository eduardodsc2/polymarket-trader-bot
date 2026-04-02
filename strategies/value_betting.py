"""
LLM-assisted mispricing detection strategy.

For each YES price update the strategy:
  1. Filters by volume, time-to-resolution, and prior entry.
  2. Optionally fetches news articles via an injected NewsFetcher.
  3. Calls an injected LLMEstimator to produce a probability estimate.
  4. Passes the estimate to DecisionEngine — trades only when edge and
     confidence are sufficient.
  5. Sizes the trade using fractional Kelly, capped at max_position_usdc.

All dependencies (LLM estimator, news fetcher, market metadata) are injected
via the constructor — no globals, no I/O inside pure methods.

LLM estimates are cached in memory per condition_id to avoid redundant API
calls within a single backtest session.
"""
from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

from loguru import logger

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import LLMEstimate, Market, OrderRequest, PortfolioSnapshot
from config.settings import settings
from llm.decision_engine import DecisionEngine
from strategies.base_strategy import BaseStrategy


class ValueBetting(BaseStrategy):
    """LLM-assisted value betting strategy.

    Args:
        market_data: condition_id → Market metadata.
        llm_estimator: Object with
            ``.estimate(condition_id, question, category, resolution_date,
                        current_price, articles) → LLMEstimate``.
        news_fetcher: Optional object with
            ``.fetch_for_market_at(question, category, before) → list[NewsArticle]``.
            If None, the LLM receives no news context.
        min_edge: Minimum absolute edge to trade (default: settings.min_edge_pct).
        kelly_fraction: Fractional Kelly multiplier (default: settings.kelly_fraction).
        max_position_usdc: Hard cap per trade in USD (default: 300.0).
        min_volume_usd: Skip markets below this volume (default: settings.llm_min_volume_usd).
        max_days_to_resolution: Skip markets resolving further away (default: 90).
    """

    name = "value_betting"

    def __init__(
        self,
        market_data: dict[str, Market],
        llm_estimator: Any,
        news_fetcher: Any | None = None,
        min_edge: float | None = None,
        kelly_fraction: float | None = None,
        max_position_usdc: float = 300.0,
        min_volume_usd: float | None = None,
        max_days_to_resolution: int = 90,
        news_skip_below_hours: float | None = None,
        max_resolution_hours: float | None = None,
    ) -> None:
        self._market_data = market_data
        self._llm_estimator = llm_estimator
        self._news_fetcher = news_fetcher
        self._min_edge = min_edge if min_edge is not None else settings.min_edge_pct
        self._kelly_fraction = (
            kelly_fraction if kelly_fraction is not None else settings.kelly_fraction
        )
        self._max_position_usdc = max_position_usdc
        self._min_volume_usd = (
            min_volume_usd if min_volume_usd is not None else settings.llm_min_volume_usd
        )
        self._max_days_to_resolution = max_days_to_resolution
        self._news_skip_below_hours = (
            news_skip_below_hours
            if news_skip_below_hours is not None
            else settings.llm_news_skip_below_hours
        )
        self._max_resolution_hours = (
            max_resolution_hours
            if max_resolution_hours is not None
            else settings.llm_max_resolution_hours
        )

        # Build token → condition_id lookup
        self._token_to_condition: dict[str, str] = {}
        for cond_id, market in market_data.items():
            if market.yes_token_id:
                self._token_to_condition[market.yes_token_id] = cond_id
            if market.no_token_id:
                self._token_to_condition[market.no_token_id] = cond_id

        # Runtime state
        self._entered: set[str] = set()
        self._llm_cache: dict[str, LLMEstimate] = {}
        self._decision_engine = DecisionEngine()

    # ── Required overrides ───────────────────────────────────────────────────

    def on_price_update(
        self,
        event: PriceUpdateEvent,
        portfolio: PortfolioSnapshot,
    ) -> list[OrderRequest]:
        cond_id = self._token_to_condition.get(event.token_id)
        if cond_id is None:
            return []

        market = self._market_data.get(cond_id)
        if market is None or market.yes_token_id != event.token_id:
            return []  # act only on YES token ticks

        if cond_id in self._entered:
            return []

        # Volume filter
        if (
            market.volume_usd is not None
            and market.volume_usd < self._min_volume_usd
        ):
            return []

        # Time-to-resolution filter
        if market.end_date is not None:
            end_dt = (
                market.end_date
                if market.end_date.tzinfo
                else market.end_date.replace(tzinfo=timezone.utc)
            )
            hours_left = (end_dt - event.timestamp).total_seconds() / 3600
            if hours_left < 0 or hours_left > self._max_resolution_hours:
                return []

        # Get (or fetch) LLM estimate
        estimate = self._llm_cache.get(cond_id)
        if estimate is None:
            try:
                estimate = self._fetch_estimate(market, event)
            except Exception as exc:
                logger.warning(
                    "ValueBetting: LLM estimate failed",
                    condition_id=cond_id,
                    error=str(exc),
                )
                return []
            self._llm_cache[cond_id] = estimate

        # DecisionEngine produces a TradeSignal or None
        signal = self._decision_engine.decide(
            estimate=estimate,
            market_price=event.price,
            condition_id=cond_id,
            token_id=market.yes_token_id or "",
            capital_usd=portfolio.total_value_usd,
        )

        if signal is None:
            return []

        # Resolve the actual token to buy
        if signal.side == "BUY":
            token_id = market.yes_token_id
            price = event.price
        else:
            # BUY NO: price ≈ 1 - YES price
            token_id = market.no_token_id
            price = 1.0 - event.price

        if token_id is None or price <= 0.0:
            return []

        size = min(signal.suggested_size_usd, self._max_position_usdc)
        if size < 1.0:
            return []

        self._entered.add(cond_id)

        logger.info(
            "ValueBetting: trade signal",
            condition_id=cond_id,
            side=signal.side,
            price=round(price, 4),
            edge=signal.edge,
            size_usd=size,
            llm_probability=estimate.probability,
            confidence=estimate.confidence,
        )

        return [
            OrderRequest(
                order_id=str(uuid.uuid4()),
                strategy=self.name,
                condition_id=cond_id,
                token_id=token_id,
                side="BUY",
                size_usd=size,
                limit_price=None,
                timestamp=event.timestamp,
            )
        ]

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        self._entered.discard(event.condition_id)
        self._llm_cache.pop(event.condition_id, None)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _should_skip_news(self, market: Market, event: PriceUpdateEvent) -> bool:
        """Pure function — returns True if the market resolves too soon for news to be useful."""
        if market.end_date is None:
            return False
        end_dt = (
            market.end_date
            if market.end_date.tzinfo
            else market.end_date.replace(tzinfo=timezone.utc)
        )
        hours_to_close = (end_dt - event.timestamp).total_seconds() / 3600
        return hours_to_close < self._news_skip_below_hours

    def _fetch_estimate(
        self, market: Market, event: PriceUpdateEvent
    ) -> LLMEstimate:
        """Fetch news (if fetcher provided) then call the LLM estimator.

        News is skipped when the market resolves in less than
        ``_news_skip_below_hours`` hours: no articles published in that window
        can be relevant, and skipping saves ~75% of input tokens.
        """
        articles: list = []
        skip_news = self._should_skip_news(market, event)
        if self._news_fetcher is not None and not skip_news:
            try:
                articles = self._news_fetcher.fetch_for_market_at(
                    question=market.question,
                    category=market.category or "base",
                    before=event.timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "ValueBetting: news fetch failed — proceeding without context",
                    error=str(exc),
                )
        elif skip_news:
            logger.debug(
                "ValueBetting: news skipped (short-window market)",
                condition_id=market.condition_id,
                news_skip_below_hours=self._news_skip_below_hours,
            )

        resolution_date = (
            market.end_date.strftime("%Y-%m-%d")
            if market.end_date is not None
            else "unknown"
        )

        return self._llm_estimator.estimate(
            condition_id=market.condition_id,
            question=market.question,
            category=market.category or "base",
            resolution_date=resolution_date,
            current_price=event.price,
            articles=articles,
        )
