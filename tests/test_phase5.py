"""
Phase 5 tests — circuit breaker, risk manager, paper executor.

Coverage targets:
  - CircuitBreaker: 100% branch coverage
  - RiskManager: all 7 rules (pass + fail path for each)
  - PaperExecutor: submit paths (approved, risk rejected, circuit open, fill error)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backtest.fill_model import FillModel
from config.schemas import (
    OrderFill,
    OrderRequest,
    PortfolioState,
    Position,
    TradeSignal,
)
from config.settings import Settings
from live.circuit_breaker import CircuitBreaker, CircuitState
from live.executor import PaperExecutor
from live.risk_manager import RiskManager, RiskCheckResult, compute_kelly_fraction


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings(
        circuit_breaker_failure_threshold=3,
        circuit_breaker_cooldown_seconds=300,
        circuit_breaker_max_positions=20,
        circuit_breaker_max_position_pct=0.05,
        circuit_breaker_daily_loss_pct=0.05,
        min_edge_pct=0.03,
        kelly_fraction=0.25,
        max_correlated_positions=3,
        min_market_volume_usd=10_000.0,
        initial_capital_usd=1_000.0,
    )


@pytest.fixture
def risk_manager(settings: Settings) -> RiskManager:
    return RiskManager(settings=settings, initial_capital=1_000.0)


@pytest.fixture
def fill_model() -> FillModel:
    return FillModel(slippage_bps=10)


@pytest.fixture
def good_signal() -> TradeSignal:
    return TradeSignal(
        strategy="test",
        condition_id="cid",
        token_id="tok",
        side="BUY",
        estimated_probability=0.70,
        market_price=0.60,
        edge=0.10,          # > min_edge_pct=0.03
        suggested_size_usd=10.0,  # < 5% of 1000
        confidence=1.0,
    )


@pytest.fixture
def empty_portfolio() -> PortfolioState:
    return PortfolioState(
        cash_usd=1_000.0,
        positions=[],
        realized_pnl=0.0,
        total_value_usd=1_000.0,
    )


@pytest.fixture
def order_request() -> OrderRequest:
    return OrderRequest(
        order_id="ord1",
        strategy="test",
        condition_id="cid",
        token_id="tok",
        side="BUY",
        size_usd=10.0,
        limit_price=None,
        timestamp=datetime.now(timezone.utc),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CircuitBreaker — 100% branch coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_attempt() is True
        assert cb.is_open() is False

    def test_single_failure_does_not_open(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 1

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_attempt() is False
        assert cb.is_open() is True

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_open_blocks_until_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=999)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_attempt() is False

    def test_open_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        cb.opened_at = time.monotonic() - 1   # simulate elapsed cooldown
        assert cb.can_attempt() is True        # triggers transition
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_probe_success_closes(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        cb.opened_at = time.monotonic() - 1
        cb.can_attempt()                      # → HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_half_open_probe_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        cb.opened_at = time.monotonic() - 1
        cb.can_attempt()                      # → HALF_OPEN
        cb.record_failure()                   # probe failed
        assert cb.state == CircuitState.OPEN

    def test_reset_clears_all_state(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.opened_at is None

    def test_can_attempt_returns_true_when_open_at_is_none(self):
        """Edge case: opened_at is None (shouldn't happen in normal flow but protected)."""
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=300)
        cb.state = CircuitState.OPEN
        cb.opened_at = None   # force None
        assert cb.can_attempt() is True   # _cooldown_elapsed returns True

    def test_is_open_false_when_closed(self):
        cb = CircuitBreaker()
        assert cb.is_open() is False

    def test_multiple_failures_then_success_resets(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED


# ══════════════════════════════════════════════════════════════════════════════
# RiskManager — all 7 rules
# ══════════════════════════════════════════════════════════════════════════════

class TestRiskManager:

    # ── compute_kelly_fraction helper ─────────────────────────────────────────

    def test_kelly_positive_edge(self):
        k = compute_kelly_fraction(p_win=0.7, odds=1.0 / 0.6)
        assert k > 0

    def test_kelly_negative_edge(self):
        k = compute_kelly_fraction(p_win=0.3, odds=1.0 / 0.6)
        assert k < 0

    def test_kelly_zero_odds(self):
        k = compute_kelly_fraction(p_win=0.7, odds=0.0)
        assert k == 0.0

    # ── check_liquidity (rule 3) ──────────────────────────────────────────────

    def test_liquidity_pass(self, risk_manager):
        r = risk_manager.check_liquidity(50_000.0)
        assert r.approved is True

    def test_liquidity_fail(self, risk_manager):
        r = risk_manager.check_liquidity(5_000.0)
        assert r.approved is False
        assert r.violation.rule == "min_market_liquidity"

    # ── Rule 4: min edge ──────────────────────────────────────────────────────

    def test_edge_pass(self, risk_manager, good_signal, empty_portfolio):
        r = risk_manager.check(good_signal, empty_portfolio)
        assert r.approved is True

    def test_edge_fail(self, risk_manager, empty_portfolio):
        signal = TradeSignal(
            strategy="test", condition_id="c", token_id="t", side="BUY",
            estimated_probability=0.62, market_price=0.60, edge=0.01,  # < 0.03
            suggested_size_usd=10.0,
        )
        r = risk_manager.check(signal, empty_portfolio)
        assert r.approved is False
        assert r.violation.rule == "min_edge"

    # ── Rule 1: max position size ─────────────────────────────────────────────

    def test_max_position_pass(self, risk_manager, empty_portfolio):
        # 5% of 1000 = 50; suggest 40 → pass
        signal = TradeSignal(
            strategy="test", condition_id="c", token_id="t", side="BUY",
            estimated_probability=0.70, market_price=0.60, edge=0.10,
            suggested_size_usd=40.0,
        )
        r = risk_manager.check(signal, empty_portfolio)
        assert r.approved is True

    def test_max_position_fail(self, risk_manager, empty_portfolio):
        signal = TradeSignal(
            strategy="test", condition_id="c", token_id="t", side="BUY",
            estimated_probability=0.70, market_price=0.60, edge=0.10,
            suggested_size_usd=200.0,   # > 5% of 1000 = 50
        )
        r = risk_manager.check(signal, empty_portfolio)
        assert r.approved is False
        assert r.violation.rule == "max_position_size"

    # ── Rule 2: Kelly cap ─────────────────────────────────────────────────────

    def test_kelly_cap_fail_negative_kelly(self, risk_manager, empty_portfolio):
        signal = TradeSignal(
            strategy="test", condition_id="c", token_id="t", side="BUY",
            estimated_probability=0.20,  # terrible odds → negative Kelly
            market_price=0.60, edge=0.05,
            suggested_size_usd=5.0,
        )
        r = risk_manager.check(signal, empty_portfolio)
        assert r.approved is False
        assert r.violation.rule == "kelly_cap"

    # ── Rule 5: max open positions ────────────────────────────────────────────

    def test_max_positions_fail(self, risk_manager, good_signal):
        positions = [
            Position(
                condition_id=f"c{i}", token_id=f"t{i}", strategy="x",
                side="BUY", size_usd=5.0, entry_price=0.5,
                opened_at=datetime.now(timezone.utc),
            )
            for i in range(20)  # already at max
        ]
        portfolio = PortfolioState(
            cash_usd=900.0, positions=positions,
            total_value_usd=1_000.0, realized_pnl=0.0,
        )
        r = risk_manager.check(good_signal, portfolio)
        assert r.approved is False
        assert r.violation.rule == "max_open_positions"

    # ── Rule 6: daily loss limit ──────────────────────────────────────────────

    def test_daily_loss_halt(self, risk_manager, good_signal):
        # initial_capital=1000, floor = 1000*(1-0.05)=950
        portfolio = PortfolioState(
            cash_usd=900.0, positions=[], total_value_usd=900.0, realized_pnl=-100.0
        )
        r = risk_manager.check(good_signal, portfolio)
        assert r.approved is False
        assert r.violation.rule == "daily_loss_limit"

    def test_daily_loss_pass(self, risk_manager, good_signal, empty_portfolio):
        r = risk_manager.check(good_signal, empty_portfolio)
        assert r.approved is True

    # ── Rule 7: correlation limit ─────────────────────────────────────────────

    def test_correlation_skip_without_category(self, risk_manager, good_signal, empty_portfolio):
        # good_signal has no category → rule is skipped
        r = risk_manager.check(good_signal, empty_portfolio)
        assert r.approved is True

    def test_correlation_fail(self, risk_manager, empty_portfolio):
        signal = TradeSignal(
            strategy="test", condition_id="c", token_id="t", side="BUY",
            estimated_probability=0.70, market_price=0.60, edge=0.10,
            suggested_size_usd=10.0,
        )
        object.__setattr__(signal, "category", "crypto")

        positions = [
            Position(
                condition_id=f"c{i}", token_id=f"t{i}", strategy="x",
                side="BUY", size_usd=5.0, entry_price=0.5,
                opened_at=datetime.now(timezone.utc),
            )
            for i in range(3)   # already at max_correlated_positions=3
        ]
        for p in positions:
            object.__setattr__(p, "category", "crypto")

        portfolio = PortfolioState(
            cash_usd=985.0, positions=positions, total_value_usd=1_000.0, realized_pnl=0.0
        )
        r = risk_manager.check(signal, portfolio)
        assert r.approved is False
        assert r.violation.rule == "correlation_limit"

    # ── total_value fallback ──────────────────────────────────────────────────

    def test_total_value_fallback(self, risk_manager):
        portfolio = PortfolioState(cash_usd=800.0, positions=[], realized_pnl=0.0)
        total = risk_manager._total_value(portfolio)
        assert total == 800.0


# ══════════════════════════════════════════════════════════════════════════════
# PaperExecutor
# ══════════════════════════════════════════════════════════════════════════════

class TestPaperExecutor:

    def _make_executor(self, settings, risk_manager, fill_model, on_fill=None):
        return PaperExecutor(
            settings=settings,
            risk_manager=risk_manager,
            fill_model=fill_model,
            on_fill=on_fill,
        )

    @pytest.mark.asyncio
    async def test_successful_fill(self, settings, risk_manager, fill_model, order_request, empty_portfolio):
        executor = self._make_executor(settings, risk_manager, fill_model)
        fill = await executor.submit(order_request, current_price=0.62, portfolio=None)
        assert fill is not None
        assert fill.side == "BUY"
        assert fill.filled_size_usd == 10.0
        assert len(executor.trades) == 1

    @pytest.mark.asyncio
    async def test_circuit_open_blocks_submission(self, settings, risk_manager, fill_model, order_request):
        executor = self._make_executor(settings, risk_manager, fill_model)
        # Force circuit to OPEN
        for _ in range(settings.circuit_breaker_failure_threshold):
            executor._breaker.record_failure()
        assert executor.circuit_state == CircuitState.OPEN
        fill = await executor.submit(order_request, current_price=0.62)
        assert fill is None
        assert len(executor.trades) == 0

    @pytest.mark.asyncio
    async def test_risk_rejection(self, settings, fill_model, order_request):
        risk = RiskManager(settings=settings, initial_capital=1_000.0)
        executor = self._make_executor(settings, risk, fill_model)
        # Portfolio below daily loss floor (< 950)
        bad_portfolio = PortfolioState(
            cash_usd=900.0, positions=[], total_value_usd=900.0, realized_pnl=-100.0
        )
        fill = await executor.submit(order_request, current_price=0.62, portfolio=bad_portfolio)
        assert fill is None

    @pytest.mark.asyncio
    async def test_fill_exception_triggers_circuit_failure(self, settings, risk_manager, order_request):
        broken_fill_model = MagicMock()
        broken_fill_model.process_order_request.side_effect = RuntimeError("boom")
        executor = self._make_executor(settings, risk_manager, broken_fill_model)
        fill = await executor.submit(order_request, current_price=0.62)
        assert fill is None
        assert executor._breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_three_failures_open_circuit(self, settings, risk_manager, order_request):
        broken_fill_model = MagicMock()
        broken_fill_model.process_order_request.side_effect = RuntimeError("boom")
        executor = self._make_executor(settings, risk_manager, broken_fill_model)
        for _ in range(settings.circuit_breaker_failure_threshold):
            await executor.submit(order_request, current_price=0.62)
        assert executor.circuit_state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_on_fill_callback_called(self, settings, risk_manager, fill_model, order_request):
        received = []

        async def on_fill(f: OrderFill) -> None:
            received.append(f)

        executor = self._make_executor(settings, risk_manager, fill_model, on_fill=on_fill)
        fill = await executor.submit(order_request, current_price=0.62)
        assert fill is not None
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_sync_on_fill_callback(self, settings, risk_manager, fill_model, order_request):
        received = []

        def sync_cb(f: OrderFill) -> None:
            received.append(f)

        executor = self._make_executor(settings, risk_manager, fill_model, on_fill=sync_cb)
        fill = await executor.submit(order_request, current_price=0.62)
        assert fill is not None
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_limit_order_not_filled_returns_none(self, settings, risk_manager, fill_model):
        req = OrderRequest(
            order_id="ord2",
            strategy="test",
            condition_id="cid",
            token_id="tok",
            side="BUY",
            size_usd=10.0,
            limit_price=0.50,   # limit below market price
            timestamp=datetime.now(timezone.utc),
        )
        executor = self._make_executor(settings, risk_manager, fill_model)
        fill = await executor.submit(req, current_price=0.65)   # market > limit
        assert fill is None   # limit not yet triggered

    def test_circuit_reset(self, settings, risk_manager, fill_model):
        executor = self._make_executor(settings, risk_manager, fill_model)
        for _ in range(settings.circuit_breaker_failure_threshold):
            executor._breaker.record_failure()
        assert executor.circuit_state == CircuitState.OPEN
        executor.circuit_reset()
        assert executor.circuit_state == CircuitState.CLOSED


# ══════════════════════════════════════════════════════════════════════════════
# Monitor
# ══════════════════════════════════════════════════════════════════════════════

class TestMonitor:

    def test_reconcile_returns_ok_report(self, settings, empty_portfolio):
        from live.monitor import Monitor
        monitor = Monitor(settings=settings)
        report = monitor.reconcile_onchain_balance("0x1234", empty_portfolio)
        assert report.ok is True
        assert report.balance_discrepancy == 0.0
        assert report.chain_id == 137

    def test_render_dashboard_plain_no_rich(self, settings, empty_portfolio):
        """render_dashboard shouldn't crash even when rich is not available."""
        from live.circuit_breaker import CircuitState
        from live.monitor import Monitor
        monitor = Monitor(settings=settings)
        with patch.dict("sys.modules", {"rich": None, "rich.console": None,
                                         "rich.table": None}):
            # Should fall back to _render_plain without raising
            monitor._render_plain(empty_portfolio, [], CircuitState.CLOSED, None)
