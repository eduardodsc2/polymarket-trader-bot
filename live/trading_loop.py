"""
Live trading loop — wires DataStream → Strategy → PaperExecutor.

Flow:
  1. GammaFetcher fetches active markets above min_volume
  2. Extract YES/NO token_ids for DataStream subscription
  3. On each price tick:
       a. Build PriceUpdateEvent
       b. Call strategy.on_price_update(event, portfolio_snapshot)
       c. Submit each returned OrderRequest to PaperExecutor
       d. Update portfolio on fill
  4. Log portfolio status every LOG_INTERVAL_SECONDS

Used by executor.py _run_paper_mode(). Same loop structure
will be reused for LiveExecutor in production.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from backtest.events import PriceUpdateEvent
from backtest.fill_model import FillModel
from config.schemas import Market, OrderFill, PortfolioSnapshot, PortfolioState
from config.settings import Settings
from data.fetchers.gamma_fetcher import GammaFetcher
from live.circuit_breaker import CircuitBreaker
from live.data_stream import DataStream
from live.db import build_engine, insert_portfolio_snapshot, make_session_factory
from live.executor import PaperExecutor
from live.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy


LOG_INTERVAL_SECONDS = 60
MAX_MARKETS = 50          # cap to avoid subscribing to thousands of tokens


class TradingLoop:
    """
    Orchestrates the live paper-trading loop.

    Args:
        settings:  Injected Settings.
        strategy:  Instantiated strategy (already configured with market_data).
        executor:  PaperExecutor (already constructed).
        markets:   Active markets fetched from Gamma API.
    """

    def __init__(
        self,
        settings: Settings,
        strategy: BaseStrategy,
        executor: PaperExecutor,
        markets: list[Market],
        session_factory: Any | None = None,
        mode: str = "paper",
    ) -> None:
        self._settings = settings
        self._strategy = strategy
        self._executor = executor
        self._session_factory = session_factory
        self._mode = mode

        # Build token_id → Market lookup
        self._token_to_market: dict[str, Market] = {}
        for m in markets:
            if m.yes_token_id:
                self._token_to_market[m.yes_token_id] = m
            if m.no_token_id:
                self._token_to_market[m.no_token_id] = m

        self._token_ids: list[str] = list(self._token_to_market.keys())

        # Mutable portfolio state — updated on every fill
        self._portfolio = PortfolioState(
            cash_usd=settings.initial_capital_usd,
            positions=[],
            realized_pnl=0.0,
            total_value_usd=settings.initial_capital_usd,
        )

        self._fills: list[OrderFill] = []
        self._tick_count: int = 0
        self._positions_value_usd: float = 0.0   # cost basis of open positions
        self._position_cost: dict[str, float] = {}  # token_id → cost paid (USD)
        self._stop_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the trading loop. Runs until stop() is called."""
        if not self._token_ids:
            logger.warning("No token IDs to subscribe to — check Gamma API response.")
            return

        logger.info(
            "TradingLoop starting | strategy={strategy} | markets={n} | tokens={t}",
            strategy=self._strategy.name,
            n=len({m.condition_id for m in self._token_to_market.values()}),
            t=len(self._token_ids),
        )

        self._strategy.on_start()

        stream = DataStream(
            token_ids=self._token_ids,
            on_price_update=self._on_price_update,
            settings=self._settings,
        )

        log_task = asyncio.create_task(self._periodic_log())

        try:
            await asyncio.gather(
                stream.run(),
                log_task,
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            stream.stop()
            log_task.cancel()
            self._strategy.on_end()
            logger.info(
                "TradingLoop stopped | fills={f} | ticks={t}",
                f=len(self._fills),
                t=self._tick_count,
            )

    def stop(self) -> None:
        self._stop_event.set()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _on_price_update(self, token_id: str, price: float) -> None:
        """Called by DataStream on every price tick."""
        if self._stop_event.is_set():
            return

        self._tick_count += 1
        market = self._token_to_market.get(token_id)
        if market is None:
            return

        event = PriceUpdateEvent(
            timestamp=datetime.now(timezone.utc),
            token_id=token_id,
            price=price,
            condition_id=market.condition_id,
        )

        snapshot = _portfolio_to_snapshot(self._portfolio, self._positions_value_usd)

        try:
            orders = self._strategy.on_price_update(event, snapshot)
        except Exception as exc:
            logger.error(
                "Strategy error on price update: {error} | token={token_id}",
                error=exc,
                token_id=token_id,
            )
            return

        for order in orders:
            fill = await self._executor.submit(
                request=order,
                current_price=price,
                portfolio=self._portfolio,
            )
            if fill is not None:
                self._apply_fill(fill, order.side, order.token_id)
                self._fills.append(fill)

    def _apply_fill(self, fill: OrderFill, side: str, token_id: str) -> None:
        """Update portfolio cash and position tracking after a fill."""
        if side == "BUY":
            self._portfolio.cash_usd -= fill.filled_size_usd
            self._positions_value_usd += fill.filled_size_usd
            self._position_cost[token_id] = fill.filled_size_usd  # track cost basis
        else:
            # Actual sell proceeds (actual_sell_usd from MarketMaker)
            cost_basis = self._position_cost.pop(token_id, fill.filled_size_usd)
            pnl = fill.filled_size_usd - cost_basis
            self._portfolio.cash_usd += fill.filled_size_usd
            self._positions_value_usd = max(0.0, self._positions_value_usd - cost_basis)
            self._portfolio.realized_pnl += pnl

        self._portfolio.total_value_usd = (
            self._portfolio.cash_usd + self._positions_value_usd
        )

    async def _periodic_log(self) -> None:
        """Log portfolio status and persist snapshot every LOG_INTERVAL_SECONDS."""
        while not self._stop_event.is_set():
            await asyncio.sleep(LOG_INTERVAL_SECONDS)
            snapshot = _portfolio_to_snapshot(self._portfolio, self._positions_value_usd)
            logger.info(
                "Portfolio | cash=${cash:.2f} | fills={fills} | ticks={ticks} | circuit={state}",
                cash=self._portfolio.cash_usd,
                fills=len(self._fills),
                ticks=self._tick_count,
                state=self._executor.circuit_state.value,
            )
            if self._session_factory is not None:
                try:
                    async with self._session_factory() as session:
                        await insert_portfolio_snapshot(session, snapshot, mode=self._mode)
                        await session.commit()
                except Exception as exc:
                    logger.error("DB snapshot persistence error: {error}", error=exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _portfolio_to_snapshot(state: PortfolioState, positions_value: float = 0.0) -> PortfolioSnapshot:
    """Derive a PortfolioSnapshot from current PortfolioState."""
    return PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc),
        cash_usd=state.cash_usd,
        positions_value_usd=positions_value,
        total_value_usd=state.total_value_usd or (state.cash_usd + positions_value),
        unrealized_pnl=0.0,
        realized_pnl=state.realized_pnl,
        open_positions=len(state.positions),
    )


def fetch_markets(settings: Settings, min_volume: float = 50_000.0) -> list[Market]:
    """
    Fetch active markets from Gamma API and filter for tradeable ones.

    Returns up to MAX_MARKETS markets that have both YES and NO token IDs,
    sorted by volume descending.
    """
    fetcher = GammaFetcher()
    markets = fetcher.get_active_markets(min_volume=min_volume)

    # Only keep markets with both token IDs (needed for strategy and DataStream)
    tradeable = [
        m for m in markets
        if m.yes_token_id and m.no_token_id
    ]

    # Sort by volume descending, cap at MAX_MARKETS
    tradeable.sort(key=lambda m: m.volume_usd or 0.0, reverse=True)
    tradeable = tradeable[:MAX_MARKETS]

    logger.info(
        "Markets loaded | total={total} | tradeable={t} | using={u}",
        total=len(markets),
        t=len([m for m in markets if m.yes_token_id and m.no_token_id]),
        u=len(tradeable),
    )
    return tradeable


async def run_paper_loop(settings: Settings) -> None:
    """
    Trading loop entry point — paper or live mode, driven by settings.bot_mode.

    Strategies by mode:
      - paper: MarketMaker (tests the full pipeline with simulated fills)
      - live:  SumToOneArb (risk-free arb, safe for real capital)
    """
    from strategies.market_maker import MarketMaker
    from strategies.sum_to_one_arb import SumToOneArb

    logger.info(
        "Starting trading loop | mode={mode} | capital=${cap}",
        mode=settings.bot_mode.upper(),
        cap=settings.initial_capital_usd,
    )

    logger.info("Fetching active markets from Gamma API...")
    markets = fetch_markets(settings, min_volume=settings.min_market_volume_usd)

    if not markets:
        logger.error("No tradeable markets found. Check Gamma API connectivity.")
        return

    market_data: dict[str, Market] = {m.condition_id: m for m in markets}

    engine = build_engine(settings)
    session_factory = make_session_factory(engine)

    if settings.bot_mode == "live":
        # SumToOneArb: risk-free, sized to capital
        max_pos = settings.initial_capital_usd * 0.40  # max 40% per arb opportunity
        strategy: BaseStrategy = SumToOneArb(
            market_data=market_data,
            min_edge=0.02,
            max_position_usdc=max_pos,
        )
        from live.executor import LiveExecutor
        risk = RiskManager(settings, initial_capital=settings.initial_capital_usd)
        executor: Any = LiveExecutor(settings=settings, risk_manager=risk, engine=engine)
        logger.info(
            "LiveExecutor ready | strategy=sum_to_one_arb | max_pos_per_arb=${max}",
            max=max_pos,
        )
    else:
        paper_strat = settings.paper_strategy.lower()
        if paper_strat == "sum_to_one_arb":
            max_pos = settings.initial_capital_usd * 0.40
            strategy = SumToOneArb(
                market_data=market_data,
                min_edge=settings.min_edge_pct,
                max_position_usdc=max_pos,
            )
            logger.info("Paper strategy: sum_to_one_arb | max_pos=${max}", max=max_pos)
        elif paper_strat == "calibration_betting":
            from strategies.calibration_betting import CalibrationBetting
            strategy = CalibrationBetting(market_data=market_data)
            logger.info("Paper strategy: calibration_betting")
        else:
            strategy = MarketMaker(market_data=market_data, order_size_usdc=20.0)
            logger.info("Paper strategy: market_maker")
        risk = RiskManager(settings, initial_capital=settings.initial_capital_usd)
        fill_model = FillModel(slippage_bps=10)
        executor = PaperExecutor(
            settings=settings,
            risk_manager=risk,
            fill_model=fill_model,
            engine=engine,
        )

    loop = TradingLoop(
        settings=settings,
        strategy=strategy,
        executor=executor,
        markets=markets,
        session_factory=session_factory,
        mode=settings.bot_mode,
    )

    try:
        await loop.run()
    except KeyboardInterrupt:
        loop.stop()
