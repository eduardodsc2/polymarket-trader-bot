"""
Phase 6 tests — LiveExecutor, DB helpers, Blockscout reconciliation, Alerter, Scheduler.

All tests are pure unit tests: no network calls, no real DB, no real CLOB.
External services are mocked via unittest.mock.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backtest.fill_model import FillModel
from config.schemas import (
    OrderFill,
    OrderRequest,
    PortfolioSnapshot,
    PortfolioState,
    ReconciliationReport,
)
from config.settings import Settings
from live.alerting import Alerter
from live.circuit_breaker import CircuitBreaker, CircuitState
from live.executor import LiveExecutor, PaperExecutor
from live.risk_manager import RiskManager


# ── Shared fixtures ────────────────────────────────────────────────────────────

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
        polymarket_private_key="0xdeadbeef",
        polymarket_api_key="key",
        polymarket_api_secret="secret",
        polymarket_api_passphrase="pass",
        telegram_bot_token="",
        telegram_chat_id="",
    )


@pytest.fixture
def risk_manager(settings):
    return RiskManager(settings=settings, initial_capital=1_000.0)


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    session.begin      = MagicMock(return_value=session)
    session.execute    = AsyncMock()
    return engine, session


@pytest.fixture
def order_request():
    return OrderRequest(
        order_id="ord-live-1",
        strategy="ValueBetting",
        condition_id="cid123",
        token_id="tok456",
        side="BUY",
        size_usd=20.0,
        limit_price=None,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def good_portfolio():
    return PortfolioState(
        cash_usd=1_000.0,
        positions=[],
        realized_pnl=0.0,
        total_value_usd=1_000.0,
    )


# ══════════════════════════════════════════════════════════════════════════════
# LiveExecutor
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveExecutor:

    def _make_executor(self, settings, risk_manager, engine, on_fill=None):
        return LiveExecutor(
            settings=settings,
            risk_manager=risk_manager,
            engine=engine,
            on_fill=on_fill,
        )

    def _mock_clob_response(self, filled_size=20.0, fill_price=0.65):
        return {
            "orderID": "clob-abc123",
            "size_matched": str(filled_size),
            "price": str(fill_price),
            "status": "MATCHED",
        }

    @pytest.mark.asyncio
    async def test_successful_live_fill(self, settings, risk_manager, mock_engine, order_request, good_portfolio):
        engine, _ = mock_engine

        executor = self._make_executor(settings, risk_manager, engine)

        mock_client = MagicMock()
        mock_client.create_market_order.return_value = MagicMock()
        mock_client.post_order.return_value = self._mock_clob_response()
        executor._clob = mock_client

        # Patch DB helpers directly so no real DB is needed
        with patch("live.executor.insert_live_order", new_callable=AsyncMock), \
             patch("live.executor.insert_trade",      new_callable=AsyncMock), \
             patch("live.executor.AsyncSession") as mock_sess_cls:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__  = AsyncMock(return_value=False)
            mock_sess.begin      = MagicMock(return_value=mock_sess)
            mock_sess_cls.return_value = mock_sess

            # portfolio=None skips risk checks (tests fill path independently)
            fill = await executor.submit(order_request, current_price=0.64, portfolio=None)

        assert fill is not None
        assert fill.side == "BUY"
        assert fill.filled_size_usd == 20.0
        assert len(executor.trades) == 1
        assert executor.circuit_state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_open_blocks_live_order(self, settings, risk_manager, mock_engine, order_request):
        engine, _ = mock_engine
        executor = self._make_executor(settings, risk_manager, engine)

        for _ in range(settings.circuit_breaker_failure_threshold):
            executor._breaker.record_failure()
        assert executor.circuit_state == CircuitState.OPEN

        fill = await executor.submit(order_request, current_price=0.64)
        assert fill is None

    @pytest.mark.asyncio
    async def test_clob_exception_increments_failure(self, settings, risk_manager, mock_engine, order_request):
        engine, _ = mock_engine
        executor = self._make_executor(settings, risk_manager, engine)

        mock_client = MagicMock()
        mock_client.create_market_order.side_effect = RuntimeError("CLOB timeout")
        executor._clob = mock_client

        fill = await executor.submit(order_request, current_price=0.64)

        assert fill is None
        assert executor._breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_insufficient_balance_rejected(self, settings, risk_manager, mock_engine, order_request):
        engine, _ = mock_engine
        executor = self._make_executor(settings, risk_manager, engine)

        poor_portfolio = PortfolioState(
            cash_usd=5.0,   # less than size_usd=20
            positions=[],
            total_value_usd=5.0,
            realized_pnl=0.0,
        )
        fill = await executor.submit(order_request, current_price=0.64, portfolio=poor_portfolio)
        assert fill is None

    @pytest.mark.asyncio
    async def test_risk_rejection_blocks_live_order(self, settings, mock_engine, order_request):
        engine, _ = mock_engine
        risk = RiskManager(settings=settings, initial_capital=1_000.0)
        executor = self._make_executor(settings, risk, engine)

        # Portfolio below daily loss floor
        bad_portfolio = PortfolioState(
            cash_usd=900.0, positions=[], total_value_usd=900.0, realized_pnl=-100.0
        )
        fill = await executor.submit(order_request, current_price=0.64, portfolio=bad_portfolio)
        assert fill is None

    @pytest.mark.asyncio
    async def test_on_fill_callback_called(self, settings, risk_manager, mock_engine, order_request):
        engine, _ = mock_engine
        received = []

        async def cb(f):
            received.append(f)

        executor = self._make_executor(settings, risk_manager, engine, on_fill=cb)
        mock_client = MagicMock()
        mock_client.create_market_order.return_value = MagicMock()
        mock_client.post_order.return_value = self._mock_clob_response()
        executor._clob = mock_client

        with patch("live.executor.insert_live_order", new_callable=AsyncMock), \
             patch("live.executor.insert_trade",      new_callable=AsyncMock), \
             patch("live.executor.AsyncSession") as mock_sess_cls:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__  = AsyncMock(return_value=False)
            mock_sess.begin      = MagicMock(return_value=mock_sess)
            mock_sess_cls.return_value = mock_sess

            fill = await executor.submit(order_request, current_price=0.64, portfolio=None)

        assert fill is not None
        assert len(received) == 1

    def test_get_clob_client_caches(self, settings, risk_manager, mock_engine):
        engine, _ = mock_engine
        executor = self._make_executor(settings, risk_manager, engine)

        mock_clob_cls = MagicMock(return_value=MagicMock())
        mock_creds_cls = MagicMock()

        c1 = executor._get_clob_client(mock_clob_cls, mock_creds_cls)
        c2 = executor._get_clob_client(mock_clob_cls, mock_creds_cls)
        assert c1 is c2
        assert mock_clob_cls.call_count == 1   # constructed only once


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers (unit — mock AsyncSession)
# ══════════════════════════════════════════════════════════════════════════════

class TestDbHelpers:

    @pytest.fixture
    def sample_fill(self):
        return OrderFill(
            order_id="ord-db-1",
            token_id="tok789",
            side="BUY",
            requested_size_usd=25.0,
            filled_size_usd=25.0,
            fill_price=0.60,
            slippage_bps=10.0,
            fee_usd=0.0,
            timestamp=datetime.now(timezone.utc),
        )

    @pytest.fixture
    def sample_snapshot(self):
        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            cash_usd=975.0,
            positions_value_usd=25.0,
            total_value_usd=1000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_positions=1,
        )

    @pytest.fixture
    def sample_report(self):
        return ReconciliationReport(
            wallet_address="0xABCDEF",
            chain_id=137,
            checked_at=datetime.now(timezone.utc),
            onchain_usdc_balance=975.12,
            internal_cash_balance=975.0,
            balance_discrepancy=0.12,
            unrecorded_transfers=[],
            unconfirmed_tx_hashes=[],
            ok=True,
        )

    @pytest.mark.asyncio
    async def test_insert_live_order_calls_execute(self, sample_fill):
        from live.db import insert_live_order
        session = AsyncMock()
        session.execute = AsyncMock()
        await insert_live_order(session, sample_fill, strategy="test", condition_id="cid")
        assert session.execute.called

    @pytest.mark.asyncio
    async def test_insert_trade_calls_execute(self, sample_fill):
        from live.db import insert_trade
        session = AsyncMock()
        session.execute = AsyncMock()
        await insert_trade(session, sample_fill, strategy="test", condition_id="cid")
        assert session.execute.called

    @pytest.mark.asyncio
    async def test_insert_portfolio_snapshot_calls_execute(self, sample_snapshot):
        from live.db import insert_portfolio_snapshot
        session = AsyncMock()
        session.execute = AsyncMock()
        await insert_portfolio_snapshot(session, sample_snapshot)
        assert session.execute.called

    @pytest.mark.asyncio
    async def test_insert_reconciliation_report_calls_execute(self, sample_report):
        from live.db import insert_reconciliation_report
        session = AsyncMock()
        session.execute = AsyncMock()
        await insert_reconciliation_report(session, sample_report)
        assert session.execute.called

    @pytest.mark.asyncio
    async def test_partial_fill_status(self, sample_fill):
        from live.db import insert_live_order
        partial_fill = sample_fill.model_copy(update={"filled_size_usd": 10.0})
        session = AsyncMock()
        session.execute = AsyncMock()
        await insert_live_order(session, partial_fill, strategy="test", condition_id="cid")
        call_args = session.execute.call_args
        params = call_args[0][1]
        assert params["status"] == "PARTIAL"

    def test_build_engine_returns_engine(self, settings):
        from live.db import build_engine
        engine = build_engine(settings)
        assert engine is not None
        # Don't actually connect — just verify it's an AsyncEngine
        from sqlalchemy.ext.asyncio import AsyncEngine
        assert isinstance(engine, AsyncEngine)

    def test_get_engine_raises_before_init(self):
        from live import db as db_module
        original = db_module._engine
        db_module._engine = None
        try:
            with pytest.raises(RuntimeError, match="not initialised"):
                db_module.get_engine()
        finally:
            db_module._engine = original


# ══════════════════════════════════════════════════════════════════════════════
# Alerter
# ══════════════════════════════════════════════════════════════════════════════

class TestAlerter:

    @pytest.mark.asyncio
    async def test_send_no_raise_when_disabled(self, settings):
        """When Telegram is not configured, send() completes without error."""
        alerter = Alerter(settings)   # no token → disabled
        # Should not raise and should not attempt HTTP call
        with patch("httpx.AsyncClient") as mock_cls:
            await alerter.send("test message")
            mock_cls.assert_not_called()   # no HTTP call when disabled

    @pytest.mark.asyncio
    async def test_send_posts_to_telegram_when_enabled(self, settings):
        settings_with_tg = settings.model_copy(update={
            "telegram_bot_token": "123:token",
            "telegram_chat_id": "456",
        })
        alerter = Alerter(settings_with_tg)
        assert alerter._enabled is True

        mock_resp = MagicMock()
        mock_resp.is_success = True

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=False)
            mock_client.post       = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            await alerter.send("hello telegram")
            mock_client.post.assert_called_once()
            call_json = mock_client.post.call_args[1]["json"]
            assert call_json["text"] == "hello telegram"

    @pytest.mark.asyncio
    async def test_alert_circuit_open(self, settings):
        alerter = Alerter(settings)
        with patch.object(alerter, "send", new_callable=AsyncMock) as mock_send:
            await alerter.alert_circuit_open("ValueBetting")
            assert mock_send.called
            assert "CIRCUIT BREAKER" in mock_send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_alert_reconciliation_ok_does_not_send(self, settings):
        alerter = Alerter(settings)
        report = ReconciliationReport(
            wallet_address="0x1", chain_id=137,
            checked_at=datetime.now(timezone.utc),
            onchain_usdc_balance=100.0, internal_cash_balance=100.0,
            balance_discrepancy=0.0, ok=True,
        )
        with patch.object(alerter, "send", new_callable=AsyncMock) as mock_send:
            await alerter.alert_reconciliation(report)
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_reconciliation_not_ok_sends(self, settings):
        alerter = Alerter(settings)
        report = ReconciliationReport(
            wallet_address="0x1", chain_id=137,
            checked_at=datetime.now(timezone.utc),
            onchain_usdc_balance=95.0, internal_cash_balance=100.0,
            balance_discrepancy=-5.0, ok=False,
        )
        with patch.object(alerter, "send", new_callable=AsyncMock) as mock_send:
            await alerter.alert_reconciliation(report)
            assert mock_send.called
            assert "RECONCILIATION FAILED" in mock_send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_alert_high_edge_skips_below_threshold(self, settings):
        alerter = Alerter(settings)
        with patch.object(alerter, "send", new_callable=AsyncMock) as mock_send:
            await alerter.alert_high_edge("cid", edge=0.05, strategy="test")
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_high_edge_sends_above_threshold(self, settings):
        alerter = Alerter(settings)
        with patch.object(alerter, "send", new_callable=AsyncMock) as mock_send:
            await alerter.alert_high_edge("cid", edge=0.15, strategy="test")
            assert mock_send.called


# ══════════════════════════════════════════════════════════════════════════════
# Monitor — Blockscout reconciliation (mocked HTTP)
# ══════════════════════════════════════════════════════════════════════════════

class TestMonitorReconciliation:

    @pytest.fixture
    def monitor(self, settings):
        from live.monitor import Monitor
        return Monitor(settings=settings)

    @pytest.fixture
    def portfolio(self):
        return PortfolioState(
            cash_usd=1_000.0, positions=[], realized_pnl=0.0, total_value_usd=1_000.0
        )

    @pytest.mark.asyncio
    async def test_reconciliation_ok_when_balance_matches(self, monitor, portfolio):
        usdc_response = {
            "items": [{
                "token": {
                    "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                    "decimals": "6",
                },
                "value": "1000000000",   # 1000 USDC.e (6 decimals)
            }]
        }
        transfers_response = {"items": []}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)

        def make_response(data):
            r = MagicMock()
            r.is_success = True
            r.json.return_value = data
            r.raise_for_status = MagicMock()
            return r

        mock_client.get.side_effect = [
            make_response(usdc_response),
            make_response(transfers_response),
        ]

        with patch("httpx.AsyncClient", return_value=mock_client):
            report = await monitor.reconcile_onchain_balance("0xwallet", portfolio)

        assert report.ok is True
        assert abs(report.balance_discrepancy) < 0.01

    @pytest.mark.asyncio
    async def test_reconciliation_fails_on_discrepancy(self, monitor, portfolio):
        usdc_response = {
            "items": [{
                "token": {
                    "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                    "decimals": "6",
                },
                "value": "500000000",   # 500 USDC.e — internal says 1000
            }]
        }
        transfers_response = {"items": []}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)

        def make_response(data):
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = data
            return r

        mock_client.get.side_effect = [
            make_response(usdc_response),
            make_response(transfers_response),
        ]

        with patch("httpx.AsyncClient", return_value=mock_client):
            report = await monitor.reconcile_onchain_balance("0xwallet", portfolio)

        assert report.ok is False
        assert report.balance_discrepancy == pytest.approx(-500.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_reconciliation_handles_api_error_gracefully(self, monitor, portfolio):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=False)
            mock_client.get.side_effect = Exception("network error")
            mock_cls.return_value = mock_client

            # Should not raise — returns a report with internal balance as fallback
            report = await monitor.reconcile_onchain_balance("0xwallet", portfolio)

        assert report is not None
        assert report.wallet_address == "0xwallet"
