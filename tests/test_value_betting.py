"""
Unit tests for Phase 4 ValueBetting strategy.

Coverage:
  - Trade signal generated when edge and confidence are sufficient
  - No signal when edge is too small
  - No signal on LOW confidence (below DecisionEngine threshold)
  - No re-entry after first position on same condition
  - Market resolution clears entered state and LLM cache
  - Volume filter skips low-liquidity markets
  - NO token selected when LLM thinks market is overpriced
  - LLM exception → no trade, no crash
  - News fetch exception → fallback to no-context estimate

All tests are pure: no network, no disk I/O (mock LLMEstimator injected).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import LLMEstimate, Market, PortfolioSnapshot
from strategies.value_betting import ValueBetting


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(day: int = 1, hour: int = 0) -> datetime:
    return datetime(2024, 6, day, hour, tzinfo=timezone.utc)


def _snapshot(cash: float = 10_000.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=_ts(),
        cash_usd=cash,
        positions_value_usd=0.0,
        total_value_usd=cash,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        open_positions=0,
    )


def _market(
    condition_id: str = "cond1",
    yes_tok: str = "yes1",
    no_tok: str = "no1",
    category: str = "crypto",
    volume_usd: float = 100_000.0,
    days_to_end: int = 30,
) -> Market:
    return Market(
        condition_id=condition_id,
        question=f"Will something happen? (market {condition_id})",
        category=category,
        end_date=_ts() + timedelta(days=days_to_end),
        resolved=False,
        outcome=None,
        volume_usd=volume_usd,
        yes_token_id=yes_tok,
        no_token_id=no_tok,
    )


def _price_event(
    token_id: str = "yes1",
    price: float = 0.50,
    condition_id: str = "cond1",
    day: int = 1,
) -> PriceUpdateEvent:
    return PriceUpdateEvent(
        timestamp=_ts(day),
        token_id=token_id,
        price=price,
        condition_id=condition_id,
    )


def _mock_estimator(probability: float = 0.75, confidence: float = 1.0) -> MagicMock:
    """Return an LLMEstimator mock that always returns a fixed estimate."""
    est = LLMEstimate(
        condition_id="cond1",
        model="mock",
        prompt_hash="mock_hash",
        probability=probability,
        confidence=confidence,
    )
    mock = MagicMock()
    mock.estimate.return_value = est
    return mock


def _strategy(
    market: Market | None = None,
    probability: float = 0.75,
    confidence: float = 1.0,
    min_edge: float = 0.05,
    max_position_usdc: float = 300.0,
    min_volume_usd: float = 0.0,
    news_fetcher=None,
) -> ValueBetting:
    m = market or _market()
    return ValueBetting(
        market_data={m.condition_id: m},
        llm_estimator=_mock_estimator(probability, confidence),
        news_fetcher=news_fetcher,
        min_edge=min_edge,
        max_position_usdc=max_position_usdc,
        min_volume_usd=min_volume_usd,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestValueBettingSignals:
    def test_generates_buy_signal_when_sufficient_edge(self):
        strat = _strategy(probability=0.75, min_edge=0.05)
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].strategy == "value_betting"
        assert orders[0].condition_id == "cond1"
        assert orders[0].token_id == "yes1"

    def test_no_signal_when_edge_too_small(self):
        # LLM=0.52, market=0.50 → edge=0.02 < min_edge=0.05
        strat = _strategy(probability=0.52, min_edge=0.05)
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert orders == []

    def test_no_signal_on_low_confidence(self):
        # confidence=0.20 < default threshold=0.33
        strat = _strategy(probability=0.80, confidence=0.20)
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert orders == []

    def test_no_token_selected_when_llm_below_market(self):
        """When LLM thinks YES is overpriced, strategy buys NO token."""
        strat = _strategy(probability=0.20, min_edge=0.05)
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert len(orders) == 1
        assert orders[0].token_id == "no1"

    def test_size_capped_at_max_position(self):
        strat = _strategy(probability=0.80, max_position_usdc=50.0)
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot(cash=10_000.0))
        assert len(orders) == 1
        assert orders[0].size_usd <= 50.0


class TestValueBettingReentry:
    def test_no_reentry_same_condition(self):
        strat = _strategy(probability=0.80)
        portfolio = _snapshot()
        orders1 = strat.on_price_update(_price_event(price=0.50, day=1), portfolio)
        orders2 = strat.on_price_update(_price_event(price=0.50, day=2), portfolio)
        assert len(orders1) == 1
        assert len(orders2) == 0  # already entered

    def test_resolution_clears_entered(self):
        strat = _strategy(probability=0.80)
        portfolio = _snapshot()
        strat.on_price_update(_price_event(price=0.50, day=1), portfolio)

        resolution = MarketResolutionEvent(
            timestamp=_ts(10),
            condition_id="cond1",
            outcome="YES",
            yes_token_id="yes1",
            no_token_id="no1",
        )
        strat.on_market_resolution(resolution)

        # Should be able to enter again after resolution
        orders = strat.on_price_update(_price_event(price=0.50, day=11), portfolio)
        assert len(orders) == 1

    def test_resolution_clears_llm_cache(self):
        strat = _strategy(probability=0.80)
        portfolio = _snapshot()
        strat.on_price_update(_price_event(price=0.50), portfolio)
        assert "cond1" in strat._llm_cache

        resolution = MarketResolutionEvent(
            timestamp=_ts(10),
            condition_id="cond1",
            outcome="YES",
        )
        strat.on_market_resolution(resolution)
        assert "cond1" not in strat._llm_cache


class TestValueBettingFilters:
    def test_volume_filter_blocks_low_volume(self):
        market = _market(volume_usd=1_000.0)
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=_mock_estimator(0.80),
            min_volume_usd=50_000.0,
            min_edge=0.05,
        )
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert orders == []

    def test_volume_filter_none_passes(self):
        """Markets with None volume should not be filtered."""
        market = _market(volume_usd=0.0)
        market = market.model_copy(update={"volume_usd": None})
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=_mock_estimator(0.80),
            min_volume_usd=50_000.0,
            min_edge=0.05,
        )
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert len(orders) == 1

    def test_days_to_resolution_filter(self):
        # Market resolves in 120 days, threshold is 90 → skip
        market = _market(days_to_end=120)
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=_mock_estimator(0.80),
            max_days_to_resolution=90,
            min_edge=0.05,
        )
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert orders == []

    def test_ignores_no_token_price_updates(self):
        strat = _strategy(probability=0.80)
        # Send a NO token update
        event = PriceUpdateEvent(
            timestamp=_ts(1),
            token_id="no1",
            price=0.50,
            condition_id="cond1",
        )
        orders = strat.on_price_update(event, _snapshot())
        assert orders == []

    def test_unknown_token_id_ignored(self):
        strat = _strategy()
        event = PriceUpdateEvent(
            timestamp=_ts(1),
            token_id="unknown_token",
            price=0.50,
        )
        orders = strat.on_price_update(event, _snapshot())
        assert orders == []


class TestValueBettingErrorHandling:
    def test_llm_exception_returns_empty(self):
        market = _market()
        bad_estimator = MagicMock()
        bad_estimator.estimate.side_effect = RuntimeError("API error")
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=bad_estimator,
            min_edge=0.05,
        )
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        assert orders == []  # graceful fallback

    def test_news_fetch_exception_falls_back(self):
        """If news fetcher raises, LLM is still called (without context)."""
        market = _market()
        bad_fetcher = MagicMock()
        bad_fetcher.fetch_for_market_at.side_effect = RuntimeError("network error")

        estimator = _mock_estimator(probability=0.80)
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=estimator,
            news_fetcher=bad_fetcher,
            min_edge=0.05,
        )
        orders = strat.on_price_update(_price_event(price=0.50), _snapshot())
        # LLM was still called despite news failure
        estimator.estimate.assert_called_once()
        assert len(orders) == 1

    def test_llm_cached_per_condition(self):
        """LLM estimator should be called once per condition, not on every tick."""
        market = _market()
        estimator = _mock_estimator(probability=0.80)
        strat = ValueBetting(
            market_data={"cond1": market},
            llm_estimator=estimator,
            min_edge=0.05,
        )
        portfolio = _snapshot()

        # First tick — LLM called
        strat.on_price_update(_price_event(price=0.50, day=1), portfolio)
        assert estimator.estimate.call_count == 1

        # Trigger resolution then re-enter: LLM called again on new entry
        strat.on_market_resolution(
            MarketResolutionEvent(timestamp=_ts(5), condition_id="cond1", outcome="YES")
        )
        strat.on_price_update(_price_event(price=0.50, day=6), portfolio)
        assert estimator.estimate.call_count == 2


class TestValueBettingMultipleMarkets:
    def test_independent_signals_per_market(self):
        m1 = _market("cond1", "yes1", "no1")
        m2 = _market("cond2", "yes2", "no2")

        # estimator returns same high-confidence estimate for both
        estimator = MagicMock()
        estimator.estimate.return_value = LLMEstimate(
            condition_id="any",
            model="mock",
            prompt_hash="hash",
            probability=0.80,
            confidence=1.0,
        )

        strat = ValueBetting(
            market_data={"cond1": m1, "cond2": m2},
            llm_estimator=estimator,
            min_edge=0.05,
        )
        portfolio = _snapshot()

        orders1 = strat.on_price_update(_price_event("yes1", 0.50, "cond1"), portfolio)
        orders2 = strat.on_price_update(_price_event("yes2", 0.50, "cond2"), portfolio)

        assert len(orders1) == 1
        assert len(orders2) == 1
        assert orders1[0].condition_id == "cond1"
        assert orders2[0].condition_id == "cond2"
