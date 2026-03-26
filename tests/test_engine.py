"""
Unit tests for the backtest engine and supporting components.

Tests are pure: no network, no DB, no filesystem.
Covers FillModel, Portfolio, and end-to-end BacktestEngine with a dummy strategy.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backtest.engine import BacktestEngine, BacktestResults
from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from backtest.fill_model import FillModel
from backtest.portfolio import Portfolio
from config.schemas import Market, OrderFill, OrderRequest, PortfolioSnapshot, PricePoint
from strategies.base_strategy import BaseStrategy


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, 0, 0, tzinfo=timezone.utc)


def _price_point(token_id: str, day: int, price: float) -> PricePoint:
    return PricePoint(token_id=token_id, timestamp=_ts(day), price=price)


def _market(condition_id: str, yes_tok: str, no_tok: str, resolved: bool = True) -> Market:
    return Market(
        condition_id=condition_id,
        question="Will X happen?",
        resolved=resolved,
        outcome="YES" if resolved else None,
        end_date=_ts(10),
        yes_token_id=yes_tok,
        no_token_id=no_tok,
    )


# ── FillModel ──────────────────────────────────────────────────────────────────

class TestFillModel:
    def test_market_buy_applies_slippage(self) -> None:
        fm = FillModel(slippage_bps=10)
        fill = fm.simulate_market_buy("tok", 100.0, 0.60, _ts(1))
        assert fill.fill_price == pytest.approx(0.60 * 1.001)
        assert fill.filled_size_usd == pytest.approx(100.0)
        assert fill.side == "BUY"

    def test_market_sell_applies_slippage(self) -> None:
        fm = FillModel(slippage_bps=10)
        fill = fm.simulate_market_sell("tok", 100.0, 0.60, _ts(1))
        assert fill.fill_price == pytest.approx(0.60 * 0.999)
        assert fill.side == "SELL"

    def test_market_buy_price_capped_at_one(self) -> None:
        fm = FillModel(slippage_bps=500)  # 5% slippage
        fill = fm.simulate_market_buy("tok", 100.0, 0.999, _ts(1))
        assert fill.fill_price <= 1.0

    def test_market_sell_price_floored_at_zero(self) -> None:
        fm = FillModel(slippage_bps=500)
        fill = fm.simulate_market_sell("tok", 100.0, 0.001, _ts(1))
        assert fill.fill_price >= 0.0

    def test_limit_buy_fills_when_price_below_limit(self) -> None:
        fm = FillModel()
        fill = fm.simulate_limit_buy("tok", 100.0, limit_price=0.55, current_price=0.50, timestamp=_ts(1))
        assert fill is not None
        assert fill.fill_price == pytest.approx(0.55)

    def test_limit_buy_does_not_fill_above_limit(self) -> None:
        fm = FillModel()
        fill = fm.simulate_limit_buy("tok", 100.0, limit_price=0.50, current_price=0.55, timestamp=_ts(1))
        assert fill is None

    def test_limit_sell_fills_when_price_above_limit(self) -> None:
        fm = FillModel()
        fill = fm.simulate_limit_sell("tok", 100.0, limit_price=0.65, current_price=0.70, timestamp=_ts(1))
        assert fill is not None
        assert fill.fill_price == pytest.approx(0.65)

    def test_limit_sell_does_not_fill_below_limit(self) -> None:
        fm = FillModel()
        fill = fm.simulate_limit_sell("tok", 100.0, limit_price=0.65, current_price=0.60, timestamp=_ts(1))
        assert fill is None

    def test_fill_is_order_fill_pydantic(self) -> None:
        fm = FillModel()
        fill = fm.simulate_market_buy("tok", 50.0, 0.5, _ts(1))
        assert isinstance(fill, OrderFill)

    def test_process_order_request_market(self) -> None:
        fm = FillModel(slippage_bps=0)
        req = OrderRequest(
            order_id="oid1",
            strategy="test",
            condition_id="c1",
            token_id="tok",
            side="BUY",
            size_usd=100.0,
            limit_price=None,
            timestamp=_ts(1),
        )
        fill = fm.process_order_request(req, current_price=0.6)
        assert fill is not None
        assert fill.fill_price == pytest.approx(0.6)

    def test_process_order_request_limit_not_triggered(self) -> None:
        fm = FillModel()
        req = OrderRequest(
            order_id="oid2",
            strategy="test",
            condition_id="c1",
            token_id="tok",
            side="BUY",
            size_usd=100.0,
            limit_price=0.50,
            timestamp=_ts(1),
        )
        fill = fm.process_order_request(req, current_price=0.60)
        assert fill is None


# ── Portfolio ──────────────────────────────────────────────────────────────────

class TestPortfolio:
    def _make_fill(
        self, side: str, size: float, price: float, token: str = "tok_yes"
    ) -> OrderFill:
        return OrderFill(
            order_id=str(uuid.uuid4()),
            token_id=token,
            side=side,
            requested_size_usd=size,
            filled_size_usd=size,
            fill_price=price,
            slippage_bps=0.0,
            fee_usd=0.0,
            timestamp=_ts(1),
        )

    def test_open_position_reduces_cash(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        fill = self._make_fill("BUY", 100.0, 0.60)
        portfolio.open_position(fill, "cond_1")
        assert portfolio.cash_usd == pytest.approx(900.0)

    def test_open_position_creates_tokens(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        fill = self._make_fill("BUY", 120.0, 0.60)
        portfolio.open_position(fill, "cond_1")
        pos = portfolio._positions["tok_yes"]
        assert pos.tokens == pytest.approx(120.0 / 0.60)

    def test_resolve_winning_position_pays_out(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        fill = self._make_fill("BUY", 60.0, 0.60)
        portfolio.open_position(fill, "cond_1")
        portfolio.resolve_position("cond_1", "YES", _ts(2))
        # tokens = 100, gross payout = 100, fee = 2, net = 98
        assert portfolio.cash_usd == pytest.approx(940.0 + 98.0)

    def test_expire_losing_position_writes_off_cost(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        fill = self._make_fill("BUY", 60.0, 0.60)
        portfolio.open_position(fill, "cond_1")
        portfolio.expire_position("cond_1")
        assert portfolio.cash_usd == pytest.approx(940.0)
        assert portfolio.realized_pnl == pytest.approx(-60.0)

    def test_mark_to_market_records_snapshot(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        fill = self._make_fill("BUY", 60.0, 0.60)
        portfolio.open_position(fill, "cond_1")
        portfolio.mark_to_market({"tok_yes": 0.70}, _ts(2))
        assert len(portfolio.snapshots) == 1
        snap = portfolio.snapshots[0]
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.positions_value_usd == pytest.approx(100 * 0.70)

    def test_get_snapshot_is_pydantic(self) -> None:
        portfolio = Portfolio(initial_capital=1000.0)
        snap = portfolio.get_snapshot(_ts(1))
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.total_value_usd == pytest.approx(1000.0)

    def test_average_down_on_second_buy(self) -> None:
        portfolio = Portfolio(initial_capital=2000.0)
        fill1 = self._make_fill("BUY", 100.0, 0.60)
        fill2 = self._make_fill("BUY", 100.0, 0.40)
        portfolio.open_position(fill1, "cond_1")
        portfolio.open_position(fill2, "cond_1")
        pos = portfolio._positions["tok_yes"]
        # tokens: 100/0.6 + 100/0.4 ≈ 166.67 + 250 = 416.67
        expected_tokens = 100 / 0.60 + 100 / 0.40
        assert pos.tokens == pytest.approx(expected_tokens)


# ── BacktestEngine end-to-end ──────────────────────────────────────────────────

class _AlwaysBuyStrategy(BaseStrategy):
    """Dummy strategy: buys $100 of YES on the first price update per market."""
    name = "always_buy"

    def __init__(self) -> None:
        self._bought: set[str] = set()

    def on_price_update(
        self, event: PriceUpdateEvent, portfolio: PortfolioSnapshot
    ) -> list[OrderRequest]:
        if event.condition_id in self._bought:
            return []
        if portfolio.cash_usd < 100:
            return []
        self._bought.add(event.condition_id)
        return [
            OrderRequest(
                order_id=str(uuid.uuid4()),
                strategy=self.name,
                condition_id=event.condition_id,
                token_id=event.token_id,
                side="BUY",
                size_usd=100.0,
                limit_price=None,
                timestamp=event.timestamp,
            )
        ]

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        self._bought.discard(event.condition_id)


class _NeverTradeStrategy(BaseStrategy):
    """Dummy strategy that never trades."""
    name = "never_trade"

    def on_price_update(self, event, portfolio) -> list[OrderRequest]:
        return []

    def on_market_resolution(self, event) -> None:
        pass


class TestBacktestEngine:
    def _make_price_data(
        self, token_id: str, days: int, price: float = 0.60
    ) -> list[PricePoint]:
        return [_price_point(token_id, d, price) for d in range(1, days + 1)]

    def test_no_trades_preserves_capital(self) -> None:
        market = _market("cond_1", "yes_tok", "no_tok")
        engine = BacktestEngine(
            strategy=_NeverTradeStrategy(),
            price_data={"yes_tok": self._make_price_data("yes_tok", 5)},
            market_data={"cond_1": market},
            initial_capital=10_000,
        )
        results = engine.run()
        assert isinstance(results, BacktestResults)
        assert results.metrics.total_trades == 0
        assert results.metrics.final_capital == pytest.approx(10_000)

    def test_always_buy_creates_trade(self) -> None:
        market = _market("cond_1", "yes_tok", "no_tok")
        engine = BacktestEngine(
            strategy=_AlwaysBuyStrategy(),
            price_data={"yes_tok": self._make_price_data("yes_tok", 5)},
            market_data={"cond_1": market},
            initial_capital=10_000,
        )
        results = engine.run()
        assert results.metrics.total_trades >= 1

    def test_seed_reproducibility(self) -> None:
        market = _market("cond_1", "yes_tok", "no_tok")
        price_data = {"yes_tok": self._make_price_data("yes_tok", 10)}

        def run_once(seed: int) -> float:
            engine = BacktestEngine(
                strategy=_AlwaysBuyStrategy(),
                price_data=price_data,
                market_data={"cond_1": market},
                initial_capital=10_000,
                random_seed=seed,
            )
            return engine.run().metrics.final_capital

        assert run_once(42) == pytest.approx(run_once(42))

    def test_different_seeds_same_result_no_randomness(self) -> None:
        """
        With a deterministic (non-random) strategy, different seeds should
        produce identical results — the strategy has no randomness.
        """
        market = _market("cond_1", "yes_tok", "no_tok")
        price_data = {"yes_tok": self._make_price_data("yes_tok", 5)}

        def run_once(seed: int) -> float:
            engine = BacktestEngine(
                strategy=_NeverTradeStrategy(),
                price_data=price_data,
                market_data={"cond_1": market},
                initial_capital=10_000,
                random_seed=seed,
            )
            return engine.run().metrics.final_capital

        assert run_once(1) == pytest.approx(run_once(99))

    def test_results_are_pydantic(self) -> None:
        market = _market("cond_1", "yes_tok", "no_tok")
        engine = BacktestEngine(
            strategy=_NeverTradeStrategy(),
            price_data={"yes_tok": self._make_price_data("yes_tok", 3)},
            market_data={"cond_1": market},
            initial_capital=5_000,
        )
        results = engine.run()
        assert isinstance(results, BacktestResults)
        from config.schemas import BacktestMetrics
        assert isinstance(results.metrics, BacktestMetrics)

    def test_winning_resolution_increases_capital(self) -> None:
        """
        Buy YES at 0.60. Market resolves YES. Expect payout > cost.
        Tokens = 100/0.60 ≈ 166.67. Net payout = 166.67 * 0.98 ≈ 163.33.
        Net gain ≈ +63.33 USDC.
        """
        market = _market("cond_1", "yes_tok", "no_tok", resolved=True)
        engine = BacktestEngine(
            strategy=_AlwaysBuyStrategy(),
            price_data={"yes_tok": self._make_price_data("yes_tok", 9, price=0.60)},
            market_data={"cond_1": market},
            initial_capital=10_000,
        )
        results = engine.run()
        assert results.metrics.final_capital > 10_000
