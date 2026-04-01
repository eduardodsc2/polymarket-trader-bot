"""
Unit tests for Phase 3 strategies.

Tests are pure: no network, no DB, no filesystem.
Each test drives the strategy's on_price_update() / on_market_resolution()
directly with synthetic events and snapshots.

Coverage:
  - SumToOneArb    — 10 tests
  - MarketMaker    — 9 tests
  - CalibrationBetting — 10 tests
  - Engine integration — 6 tests (end-to-end backtest runs)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backtest.engine import BacktestEngine, BacktestResults
from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import Market, OrderRequest, PortfolioSnapshot, PricePoint
from strategies.calibration_betting import CalibrationBetting, DEFAULT_BASE_RATES
from strategies.market_maker import MarketMaker
from strategies.sum_to_one_arb import SumToOneArb


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=timezone.utc)


def _snapshot(
    cash: float = 10_000.0,
    positions_value: float = 0.0,
    realized_pnl: float = 0.0,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=_ts(1),
        cash_usd=cash,
        positions_value_usd=positions_value,
        total_value_usd=cash + positions_value,
        unrealized_pnl=0.0,
        realized_pnl=realized_pnl,
        open_positions=0,
    )


def _market(
    condition_id: str,
    yes_tok: str,
    no_tok: str,
    category: str | None = None,
    resolved: bool = True,
    days_to_end: int = 10,
) -> Market:
    return Market(
        condition_id=condition_id,
        question="Will X happen?",
        category=category,
        resolved=resolved,
        outcome="YES" if resolved else None,
        end_date=_ts(days_to_end),
        yes_token_id=yes_tok,
        no_token_id=no_tok,
    )


def _price_event(
    token_id: str,
    price: float,
    condition_id: str = "cond_1",
    day: int = 1,
) -> PriceUpdateEvent:
    return PriceUpdateEvent(
        timestamp=_ts(day),
        token_id=token_id,
        price=price,
        condition_id=condition_id,
    )


def _resolution_event(condition_id: str = "cond_1", outcome: str = "YES") -> MarketResolutionEvent:
    return MarketResolutionEvent(
        timestamp=_ts(10),
        condition_id=condition_id,
        outcome=outcome,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )


def _price_points(token_id: str, prices: list[float]) -> list[PricePoint]:
    return [
        PricePoint(token_id=token_id, timestamp=_ts(i + 1), price=p)
        for i, p in enumerate(prices)
    ]


# ── SumToOneArb ─────────────────────────────────────────────────────────────────

class TestSumToOneArb:
    def _strategy(self, min_edge: float = 0.02) -> SumToOneArb:
        market_data = {"cond_1": _market("cond_1", "yes_tok", "no_tok")}
        return SumToOneArb(market_data=market_data, min_edge=min_edge, max_position_usdc=500.0)

    def test_no_orders_until_both_prices_known(self) -> None:
        """Only YES price known — no order yet."""
        s = self._strategy()
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        assert orders == []

    def test_no_orders_when_edge_below_threshold(self) -> None:
        """YES=0.60 + NO=0.39 = 0.99 → edge=0.01 < min_edge=0.02."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.39), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        assert orders == []

    def test_orders_emitted_when_edge_exceeds_threshold(self) -> None:
        """YES=0.60 + NO=0.35 = 0.95 → edge=0.05 > min_edge=0.02."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        assert len(orders) == 2

    def test_order_sides_are_both_buy(self) -> None:
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        assert all(o.side == "BUY" for o in orders)

    def test_current_token_is_market_order(self) -> None:
        """The token whose price just updated should be a market order (limit_price=None)."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        yes_order = next(o for o in orders if o.token_id == "yes_tok")
        assert yes_order.limit_price is None

    def test_other_token_is_limit_order(self) -> None:
        """The non-updating token should be a limit order at last known price."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        no_order = next(o for o in orders if o.token_id == "no_tok")
        assert no_order.limit_price == pytest.approx(0.35)

    def test_no_duplicate_entry(self) -> None:
        """Second price update for same condition does not emit more orders."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.58), _snapshot())
        assert orders == []

    def test_re_entry_after_resolution(self) -> None:
        """After resolution, strategy can enter the same condition again."""
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        s.on_market_resolution(_resolution_event())
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        assert len(orders) == 2

    def test_no_orders_when_cash_insufficient(self) -> None:
        """If cash < 2 USD, total_budget < 2 USD → no order emitted."""
        s = self._strategy()
        snap = _snapshot(cash=1.0)
        s.on_price_update(_price_event("no_tok", 0.35), snap)
        orders = s.on_price_update(_price_event("yes_tok", 0.60), snap)
        assert orders == []

    def test_token_ids_correct_in_orders(self) -> None:
        s = self._strategy()
        s.on_price_update(_price_event("no_tok", 0.35), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.60), _snapshot())
        token_ids = {o.token_id for o in orders}
        assert token_ids == {"yes_tok", "no_tok"}


# ── MarketMaker ─────────────────────────────────────────────────────────────────

class TestMarketMaker:
    def _strategy(
        self,
        base_spread: float = 0.04,
        window: int = 3,
        order_size: float = 200.0,
    ) -> MarketMaker:
        market_data = {"cond_1": _market("cond_1", "yes_tok", "no_tok")}
        return MarketMaker(
            market_data=market_data,
            base_spread=base_spread,
            window=window,
            order_size_usdc=order_size,
        )

    def _feed_prices(self, s: MarketMaker, prices: list[float]) -> list[list[OrderRequest]]:
        snap = _snapshot()
        results = []
        for i, p in enumerate(prices):
            ev = _price_event("yes_tok", p, day=i + 1)
            results.append(s.on_price_update(ev, snap))
        return results

    def test_no_orders_before_window_fills(self) -> None:
        """With window=3, first 2 ticks produce no orders."""
        s = self._strategy()
        results = self._feed_prices(s, [0.50, 0.49])
        assert all(r == [] for r in results)

    def test_unknown_token_returns_empty(self) -> None:
        s = self._strategy()
        ev = _price_event("unknown_tok", 0.50)
        assert s.on_price_update(ev, _snapshot()) == []

    def test_buy_when_price_drops_below_threshold(self) -> None:
        """After window fills with 0.50, a drop to 0.47 triggers a BUY."""
        s = self._strategy(base_spread=0.04, window=3)
        # Fill window: fair_value = 0.50, buy_threshold = 0.48
        self._feed_prices(s, [0.50, 0.50, 0.50])
        ev = _price_event("yes_tok", 0.47, day=4)
        orders = s.on_price_update(ev, _snapshot())
        assert len(orders) == 1
        assert orders[0].side == "BUY"

    def test_no_buy_when_price_above_threshold(self) -> None:
        """Price at fair_value does not trigger a buy."""
        s = self._strategy(base_spread=0.04, window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        ev = _price_event("yes_tok", 0.50, day=4)
        orders = s.on_price_update(ev, _snapshot())
        assert orders == []

    def test_sell_after_price_recovers(self) -> None:
        """Enter at 0.47, then sell when price recovers to 0.47 + 0.04 = 0.51."""
        s = self._strategy(base_spread=0.04, window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        # Buy at 0.47
        s.on_price_update(_price_event("yes_tok", 0.47, day=4), _snapshot())
        # Recovery — price rises to 0.52 (>= 0.47 + 0.04 = 0.51)
        ev = _price_event("yes_tok", 0.52, day=5)
        orders = s.on_price_update(ev, _snapshot(positions_value=200.0))
        assert len(orders) == 1
        assert orders[0].side == "SELL"

    def test_no_sell_before_target(self) -> None:
        """Price recovers to 0.49, which is below entry(0.47) + spread(0.04) = 0.51."""
        s = self._strategy(base_spread=0.04, window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        s.on_price_update(_price_event("yes_tok", 0.47, day=4), _snapshot())
        ev = _price_event("yes_tok", 0.49, day=5)
        orders = s.on_price_update(ev, _snapshot(positions_value=200.0))
        assert orders == []

    def test_inventory_cap_prevents_over_buying(self) -> None:
        """positions_value_usd >= max_inventory_pct * total_value → no BUY."""
        s = self._strategy(base_spread=0.04, window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        # Portfolio already at max inventory (40% of 10k = 4000, positions_value = 4001)
        snap = _snapshot(cash=5_999.0, positions_value=4_001.0)
        ev = _price_event("yes_tok", 0.47, day=4)
        orders = s.on_price_update(ev, snap)
        assert orders == []

    def test_no_buy_when_insufficient_cash(self) -> None:
        s = self._strategy(order_size=500.0, window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        snap = _snapshot(cash=100.0)  # less than order_size
        ev = _price_event("yes_tok", 0.47, day=4)
        orders = s.on_price_update(ev, snap)
        assert orders == []

    def test_resolution_clears_state(self) -> None:
        """After resolution, price history is cleared and window must refill."""
        s = self._strategy(window=3)
        self._feed_prices(s, [0.50, 0.50, 0.50])
        s.on_market_resolution(_resolution_event())
        # After resolution, only 2 ticks — no orders yet
        results = self._feed_prices(s, [0.47, 0.47])
        assert all(r == [] for r in results)

    def test_no_token_id_cross_trades(self) -> None:
        """
        Regression: buying YES token must NOT trigger SELL of NO token.

        With the old condition_id-based tracking, buying yes_tok set
        _in_position["cond_1"]. Then when no_tok ticked at a high price,
        the strategy would emit a SELL on no_tok (which was never bought).
        The fix keys _in_position by token_id, so each token is independent.
        """
        market_data = {"cond_1": _market("cond_1", "yes_tok", "no_tok")}
        s = MarketMaker(market_data=market_data, base_spread=0.04, window=3, order_size_usdc=200.0)
        snap = _snapshot()

        # Fill window for yes_tok and trigger a BUY at 0.47
        for p in [0.50, 0.50, 0.50, 0.47]:
            s.on_price_update(_price_event("yes_tok", p), snap)

        assert "yes_tok" in s._in_position
        assert "no_tok" not in s._in_position  # no_tok was never traded

        # Feed a high price on no_tok — should NOT trigger a SELL (no position)
        # (no_tok hasn't even filled its price window yet)
        orders = s.on_price_update(_price_event("no_tok", 0.90), snap)
        assert orders == [], "no_tok must not trigger SELL when only yes_tok was bought"

    def test_sell_size_reflects_actual_proceeds(self) -> None:
        """
        SELL size_usd must be tokens_bought × sell_price, not the fixed BUY size.

        BUY at 0.40 with $200: tokens = 200 / 0.40 = 500
        SELL at 0.50: proceeds = 500 × 0.50 = $250  (not $200)
        """
        market_data = {"cond_1": _market("cond_1", "yes_tok", "no_tok")}
        s = MarketMaker(market_data=market_data, base_spread=0.04, window=3, order_size_usdc=200.0)
        snap = _snapshot()

        # Build window and trigger BUY at 0.40
        for p in [0.44, 0.44, 0.44, 0.40]:  # fair=0.44, threshold=0.42, 0.40 < 0.42 → BUY
            s.on_price_update(_price_event("yes_tok", p), snap)

        entry = s._entry_price.get("yes_tok")
        assert entry is not None
        assert entry == pytest.approx(0.40, abs=0.01)

        # Now trigger SELL: sell_target = 0.40 + 0.04 = 0.44
        orders = s.on_price_update(_price_event("yes_tok", 0.44), snap)
        assert len(orders) == 1
        sell_order = orders[0]
        assert sell_order.side == "SELL"
        expected_proceeds = (200.0 / entry) * 0.44
        assert sell_order.size_usd == pytest.approx(expected_proceeds, rel=0.01)


# ── CalibrationBetting ─────────────────────────────────────────────────────────

class TestCalibrationBetting:
    def _strategy(
        self,
        category: str | None = "politics",
        min_edge: float = 0.05,
        max_days: int = 90,
    ) -> CalibrationBetting:
        market_data = {
            "cond_1": _market("cond_1", "yes_tok", "no_tok", category=category)
        }
        return CalibrationBetting(
            market_data=market_data,
            min_edge=min_edge,
            kelly_fraction=0.25,
            max_position_usdc=300.0,
            max_days_to_resolution=max_days,
        )

    def test_no_action_on_no_token_tick(self) -> None:
        """Strategy only acts on YES token price updates."""
        s = self._strategy()
        orders = s.on_price_update(_price_event("no_tok", 0.70), _snapshot())
        assert orders == []

    def test_no_action_when_edge_below_threshold(self) -> None:
        """Politics base_rate=0.45, price=0.48 → |edge|=0.03 < min_edge=0.05."""
        s = self._strategy(category="politics", min_edge=0.05)
        orders = s.on_price_update(_price_event("yes_tok", 0.48), _snapshot())
        assert orders == []

    def test_bet_no_when_yes_overpriced(self) -> None:
        """Politics base_rate=0.45, price=0.70 → YES overpriced → buy NO."""
        s = self._strategy(category="politics", min_edge=0.05)
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert len(orders) == 1
        assert orders[0].token_id == "no_tok"
        assert orders[0].side == "BUY"

    def test_bet_yes_when_yes_underpriced(self) -> None:
        """crypto base_rate=0.50, price=0.30 → YES underpriced → buy YES."""
        s = self._strategy(category="crypto", min_edge=0.05)
        orders = s.on_price_update(_price_event("yes_tok", 0.30), _snapshot())
        assert len(orders) == 1
        assert orders[0].token_id == "yes_tok"
        assert orders[0].side == "BUY"

    def test_no_duplicate_entry(self) -> None:
        s = self._strategy(category="politics")
        s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert orders == []

    def test_re_entry_after_resolution(self) -> None:
        s = self._strategy(category="politics")
        s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        s.on_market_resolution(_resolution_event())
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert len(orders) == 1

    def test_default_base_rates_loaded(self) -> None:
        s = CalibrationBetting(market_data={})
        for key in DEFAULT_BASE_RATES:
            assert key in s._base_rates

    def test_custom_base_rate_overrides_default(self) -> None:
        market_data = {"cond_1": _market("cond_1", "yes_tok", "no_tok", category="crypto")}
        s = CalibrationBetting(
            market_data=market_data,
            base_rates={"crypto": 0.80},   # override default 0.50
            min_edge=0.05,
        )
        # crypto = 0.80, price = 0.70 → edge = +0.10 → buy YES
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert len(orders) == 1
        assert orders[0].token_id == "yes_tok"

    def test_skips_markets_beyond_horizon(self) -> None:
        """Market resolves in ~200 days — beyond max_days=90 → no order."""
        far_future = datetime(2024, 8, 1, tzinfo=timezone.utc)  # ~210 days from Jan 1
        market_data = {
            "cond_1": Market(
                condition_id="cond_1",
                question="Will X happen?",
                category="politics",
                resolved=False,
                end_date=far_future,
                yes_token_id="yes_tok",
                no_token_id="no_tok",
            )
        }
        s = CalibrationBetting(
            market_data=market_data, min_edge=0.05, max_days_to_resolution=90
        )
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert orders == []

    def test_kelly_size_positive_and_capped(self) -> None:
        """Size must be > 0 and <= max_position_usdc."""
        s = self._strategy(category="politics")
        orders = s.on_price_update(_price_event("yes_tok", 0.70), _snapshot())
        assert len(orders) == 1
        size = orders[0].size_usd
        assert 0 < size <= 300.0


# ── Engine integration tests ────────────────────────────────────────────────────

class TestStrategyIntegration:
    """
    End-to-end backtest runs through BacktestEngine.

    These tests verify that strategies produce meaningful results when
    connected to the full engine + portfolio + fill model pipeline.
    """

    def _run_engine(
        self,
        strategy,
        price_data: dict[str, list[PricePoint]],
        market_data: dict[str, Market],
        capital: float = 10_000.0,
    ) -> BacktestResults:
        engine = BacktestEngine(
            strategy=strategy,
            price_data=price_data,
            market_data=market_data,
            initial_capital=capital,
        )
        return engine.run()

    # ── SumToOneArb integration ─────────────────────────────────────────────────

    def test_arb_both_legs_resolved_correctly(self) -> None:
        """
        YES=0.60, NO=0.35 → edge=0.05. Budget=$100 split proportionally (same token count).
          YES leg = $100 * 0.60/0.95 ≈ $63.16 → ~105 YES tokens
          NO  leg = $100 * 0.35/0.95 ≈ $36.84 → ~105 NO tokens
        Market resolves YES:
          YES payout = 105 * 0.98 ≈ $103.06 (net after 2% fee)
          NO  expired → loss ≈ $36.84
        Net gain ≈ +$3.06 → final_capital > 10_000.
        """
        market = _market("cond_1", "yes_tok", "no_tok", resolved=True)
        yes_prices = _price_points("yes_tok", [0.60] * 5)
        no_prices = _price_points("no_tok", [0.35] * 5)
        strategy = SumToOneArb(
            market_data={"cond_1": market},
            min_edge=0.02,
            max_position_usdc=100.0,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": yes_prices, "no_tok": no_prices},
            market_data={"cond_1": market},
        )
        assert results.metrics.final_capital > 10_000.0

    def test_arb_losing_leg_does_not_double_pay(self) -> None:
        """
        Verify the per-token resolution fix: the losing NO leg must NOT receive
        a $1 payout. If the engine incorrectly paid both legs, final_capital
        would be >> 10_000 (it would be ~10_000 + 2 * payout).
        This test bounds the gain to be realistic (< $200 net on $100 per leg).
        """
        market = _market("cond_1", "yes_tok", "no_tok", resolved=True)
        yes_prices = _price_points("yes_tok", [0.60] * 5)
        no_prices = _price_points("no_tok", [0.35] * 5)
        strategy = SumToOneArb(
            market_data={"cond_1": market},
            min_edge=0.02,
            max_position_usdc=100.0,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": yes_prices, "no_tok": no_prices},
            market_data={"cond_1": market},
        )
        # Realistic bound: gain ≤ 2 * 100 (never more than $200 net on $200 invested)
        assert results.metrics.final_capital < 10_200.0

    # ── MarketMaker integration ─────────────────────────────────────────────────

    def test_market_maker_creates_trades_on_volatile_market(self) -> None:
        """Oscillating price triggers buy-low/sell-high cycle."""
        market = _market("cond_1", "yes_tok", "no_tok", resolved=True)
        # Oscillate: 0.50 base, dips to 0.46, recovers
        prices = [0.50, 0.50, 0.50, 0.46, 0.46, 0.52, 0.52, 0.50, 0.50]
        strategy = MarketMaker(
            market_data={"cond_1": market},
            base_spread=0.04,
            window=3,
            order_size_usdc=200.0,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": _price_points("yes_tok", prices)},
            market_data={"cond_1": market},
        )
        assert results.metrics.total_trades >= 1

    def test_market_maker_no_trades_on_flat_market(self) -> None:
        """Constant price never drops below buy threshold — zero trades."""
        market = _market("cond_1", "yes_tok", "no_tok", resolved=True)
        prices = [0.50] * 8
        strategy = MarketMaker(
            market_data={"cond_1": market},
            base_spread=0.04,
            window=3,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": _price_points("yes_tok", prices)},
            market_data={"cond_1": market},
        )
        assert results.metrics.total_trades == 0

    # ── CalibrationBetting integration ─────────────────────────────────────────

    def test_calibration_bets_no_on_overpriced_politics_market(self) -> None:
        """
        Politics base_rate=0.45, market prices YES at 0.70 → buy NO.
        Market resolves YES (bad for NO bet) → capital should decrease.
        """
        market = _market("cond_1", "yes_tok", "no_tok", category="politics", resolved=True)
        market_data = {"cond_1": market}
        strategy = CalibrationBetting(
            market_data=market_data,
            min_edge=0.05,
            max_position_usdc=300.0,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": _price_points("yes_tok", [0.70] * 5)},
            market_data=market_data,
        )
        # Strategy bought NO; market resolved YES → loss expected
        assert results.metrics.total_trades >= 1
        assert results.metrics.final_capital < 10_000.0

    def test_calibration_wins_when_prediction_correct(self) -> None:
        """
        Politics base_rate=0.45, YES prices at 0.80 → buy NO.
        Market resolves NO (strategy was right) → capital increases.
        """
        market = Market(
            condition_id="cond_1",
            question="Will X happen?",
            category="politics",
            resolved=True,
            outcome="NO",           # ← strategy's bet wins
            end_date=_ts(10),
            yes_token_id="yes_tok",
            no_token_id="no_tok",
        )
        strategy = CalibrationBetting(
            market_data={"cond_1": market},
            min_edge=0.05,
            max_position_usdc=300.0,
        )
        results = self._run_engine(
            strategy,
            price_data={"yes_tok": _price_points("yes_tok", [0.80] * 5)},
            market_data={"cond_1": market},
        )
        assert results.metrics.final_capital > 10_000.0
