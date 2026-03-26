"""
Unit tests for backtest/metrics.py.

All tests are pure: no network, no DB, no filesystem.
Known-input / known-output validation for every metric function.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backtest.metrics import (
    brier_score,
    cagr,
    compute_metrics,
    kelly_fraction,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)
from config.schemas import PortfolioSnapshot, Trade


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _ts(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _snapshot(ts: datetime, value: float) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=ts,
        cash_usd=value,
        positions_value_usd=0.0,
        total_value_usd=value,
        unrealized_pnl=0.0,
        realized_pnl=value - 10_000,
        open_positions=0,
    )


def _sell_trade(size: float, price: float) -> Trade:
    return Trade(
        strategy="test",
        condition_id="cond_1",
        token_id="tok_yes",
        side="SELL",
        size_usd=size,
        price=price,
        fee_usd=0.0,
        mode="backtest",
        executed_at=_ts(2024),
    )


# ── sharpe_ratio ───────────────────────────────────────────────────────────────

class TestSharpeRatio:
    def test_zero_returns(self) -> None:
        assert sharpe_ratio([0.0, 0.0, 0.0]) == pytest.approx(0.0)

    def test_positive_constant_returns(self) -> None:
        # Constant excess returns have std=0 → Sharpe=0
        assert sharpe_ratio([0.01, 0.01, 0.01]) == pytest.approx(0.0)

    def test_positive_varied(self) -> None:
        returns = [0.01, -0.005, 0.02, 0.015, -0.01]
        ratio = sharpe_ratio(returns)
        assert ratio > 0  # mean positive, sign should be positive

    def test_all_negative(self) -> None:
        returns = [-0.01, -0.02, -0.015]
        ratio = sharpe_ratio(returns)
        assert ratio < 0

    def test_single_return(self) -> None:
        assert sharpe_ratio([0.05]) == pytest.approx(0.0)

    def test_empty(self) -> None:
        assert sharpe_ratio([]) == pytest.approx(0.0)

    def test_annualisation_returns_float(self) -> None:
        returns = [0.001] * 50 + [-0.001] * 50
        r = sharpe_ratio(returns)
        assert isinstance(r, float)


# ── sortino_ratio ──────────────────────────────────────────────────────────────

class TestSortinoRatio:
    def test_no_negative_returns(self) -> None:
        assert sortino_ratio([0.01, 0.02, 0.03]) == pytest.approx(0.0)

    def test_mixed(self) -> None:
        returns = [0.02, -0.01, 0.03, -0.005, 0.015]
        ratio = sortino_ratio(returns)
        assert ratio > 0

    def test_all_negative(self) -> None:
        returns = [-0.01, -0.02, -0.015]
        ratio = sortino_ratio(returns)
        assert ratio < 0

    def test_sortino_gt_sharpe_when_losses_small(self) -> None:
        returns = [0.05, -0.001, 0.04, -0.002, 0.06]
        assert sortino_ratio(returns) > sharpe_ratio(returns)


# ── max_drawdown ───────────────────────────────────────────────────────────────

class TestMaxDrawdown:
    def test_no_drawdown(self) -> None:
        assert max_drawdown([100, 110, 120, 130]) == pytest.approx(0.0)

    def test_50_pct_drawdown(self) -> None:
        dd = max_drawdown([100, 90, 80, 50, 60])
        assert dd == pytest.approx(0.5)

    def test_single_point(self) -> None:
        assert max_drawdown([100]) == pytest.approx(0.0)

    def test_empty(self) -> None:
        assert max_drawdown([]) == pytest.approx(0.0)

    def test_recovery_still_reports_max(self) -> None:
        dd = max_drawdown([100, 80, 90, 100, 110])
        assert dd == pytest.approx(0.2)

    def test_multiple_drawdowns_picks_max(self) -> None:
        # First: 100→90=10%, second: 110→70≈36.4%
        dd = max_drawdown([100, 90, 110, 70, 80])
        assert dd == pytest.approx((110 - 70) / 110)


# ── cagr ───────────────────────────────────────────────────────────────────────

class TestCAGR:
    def test_double_in_one_year(self) -> None:
        assert cagr(100, 200, 1.0) == pytest.approx(1.0)

    def test_double_in_two_years(self) -> None:
        assert cagr(100, 200, 2.0) == pytest.approx(2 ** 0.5 - 1)

    def test_zero_gain(self) -> None:
        assert cagr(100, 100, 1.0) == pytest.approx(0.0)

    def test_loss(self) -> None:
        assert cagr(100, 50, 1.0) == pytest.approx(-0.5)

    def test_zero_initial_returns_zero(self) -> None:
        assert cagr(0, 100, 1.0) == pytest.approx(0.0)

    def test_zero_years_returns_zero(self) -> None:
        assert cagr(100, 200, 0.0) == pytest.approx(0.0)


# ── brier_score ────────────────────────────────────────────────────────────────

class TestBrierScore:
    def test_perfect_prediction(self) -> None:
        assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)

    def test_worst_prediction(self) -> None:
        assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)

    def test_uniform_50_50(self) -> None:
        assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)

    def test_empty(self) -> None:
        assert brier_score([], []) == pytest.approx(0.0)


# ── kelly_fraction ─────────────────────────────────────────────────────────────

class TestKellyFraction:
    def test_50_50_even_odds(self) -> None:
        # p=0.5, b=1 → Kelly=(0.5*1 - 0.5)/1 = 0
        assert kelly_fraction(0.5, 1.0) == pytest.approx(0.0)

    def test_edge(self) -> None:
        # p=0.6, b=1 → (0.6 - 0.4)/1 = 0.2
        assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)

    def test_zero_payoff_returns_zero(self) -> None:
        assert kelly_fraction(0.9, 0.0) == pytest.approx(0.0)

    def test_certainty(self) -> None:
        assert kelly_fraction(1.0, 1.0) == pytest.approx(1.0)


# ── win_rate & profit_factor ───────────────────────────────────────────────────

class TestWinRate:
    def test_all_wins(self) -> None:
        trades = [_sell_trade(100, 0.9), _sell_trade(50, 0.8)]
        assert win_rate(trades) == pytest.approx(1.0)

    def test_empty(self) -> None:
        assert win_rate([]) == pytest.approx(0.0)

    def test_only_buy_trades(self) -> None:
        buy = Trade(
            strategy="t", condition_id="c", token_id="t", side="BUY",
            size_usd=100, price=0.5, fee_usd=0, mode="backtest",
            executed_at=_ts(2024),
        )
        assert win_rate([buy]) == pytest.approx(0.0)


class TestProfitFactor:
    def test_no_losses_returns_zero(self) -> None:
        trades = [_sell_trade(100, 0.9)]
        assert profit_factor(trades) == pytest.approx(0.0)

    def test_empty_returns_zero(self) -> None:
        assert profit_factor([]) == pytest.approx(0.0)


# ── compute_metrics ────────────────────────────────────────────────────────────

class TestComputeMetrics:
    def test_flat_equity_returns_zeros(self) -> None:
        snapshots = [_snapshot(_ts(2024, 1, d), 10_000) for d in range(1, 11)]
        m = compute_metrics(
            strategy_name="test",
            snapshots=snapshots,
            trades=[],
            initial_capital=10_000,
            start_date=_ts(2024, 1, 1),
            end_date=_ts(2024, 1, 10),
        )
        assert m.total_return_pct == pytest.approx(0.0)
        assert m.sharpe_ratio == pytest.approx(0.0)
        assert m.max_drawdown_pct == pytest.approx(0.0)
        assert m.total_trades == 0
        assert m.strategy == "test"

    def test_growing_equity_positive_sharpe(self) -> None:
        values = [10_000 * (1.001 ** d) for d in range(100)]
        snapshots = [_snapshot(_ts(2024, 1, 1), v) for v in values]
        m = compute_metrics(
            strategy_name="growth",
            snapshots=snapshots,
            trades=[],
            initial_capital=10_000,
            start_date=_ts(2024, 1, 1),
            end_date=_ts(2024, 4, 9),
        )
        assert m.total_return_pct > 0
        assert m.sharpe_ratio > 0
        assert m.max_drawdown_pct == pytest.approx(0.0)

    def test_empty_snapshots_returns_initial_capital(self) -> None:
        m = compute_metrics(
            strategy_name="empty",
            snapshots=[],
            trades=[],
            initial_capital=10_000,
            start_date=_ts(2024, 1, 1),
            end_date=_ts(2024, 6, 1),
        )
        assert m.final_capital == pytest.approx(10_000)
        assert m.total_return_pct == pytest.approx(0.0)

    def test_returns_pydantic_model(self) -> None:
        from config.schemas import BacktestMetrics
        snapshots = [_snapshot(_ts(2024, 1, d), 10_000) for d in range(1, 5)]
        m = compute_metrics("s", snapshots, [], 10_000, _ts(2024, 1, 1), _ts(2024, 1, 4))
        assert isinstance(m, BacktestMetrics)
