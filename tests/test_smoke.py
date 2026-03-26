"""
Phase 0 smoke tests — verify the Python environment and imports are healthy.
All heavy integration tests live in their own modules (test_metrics, test_engine, etc.).
"""
from __future__ import annotations


def test_config_imports() -> None:
    """Settings and schemas must import without errors."""
    from config.settings import settings
    from config.schemas import (
        BacktestMetrics,
        LLMEstimate,
        Market,
        NewsArticle,
        NewsFeatures,
        OrderbookSnapshot,
        PortfolioState,
        PricePoint,
        Trade,
        TradeSignal,
    )
    assert settings.random_seed == 42
    assert settings.circuit_breaker_max_positions == 20


def test_strategy_base_importable() -> None:
    from strategies.base_strategy import BaseStrategy
    assert BaseStrategy is not None


def test_strategy_stubs_importable() -> None:
    from strategies.sum_to_one_arb import SumToOneArb
    from strategies.market_maker import MarketMaker
    from strategies.value_betting import ValueBetting
    from strategies.momentum import Momentum
    assert SumToOneArb.name == "sum_to_one_arb"
    assert MarketMaker.name == "market_maker"


def test_backtest_stubs_importable() -> None:
    from backtest.engine import BacktestEngine
    from backtest.metrics import kelly_fraction
    assert BacktestEngine is not None
    # kelly_fraction is the only non-stub function in metrics.py
    result = kelly_fraction(win_prob=0.6, win_payoff=1.0)
    assert abs(result - 0.2) < 1e-9


def test_fetcher_stubs_importable() -> None:
    from data.fetchers.gamma_fetcher import GammaFetcher
    from data.fetchers.clob_fetcher import CLOBFetcher
    from data.fetchers.pmxt_fetcher import PmxtDownloader
    assert GammaFetcher.BASE_URL == "https://gamma-api.polymarket.com"
    assert CLOBFetcher.BASE_URL == "https://clob.polymarket.com"
