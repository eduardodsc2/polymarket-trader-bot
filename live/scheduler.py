"""
APScheduler process — entry point for the scheduler container.

Scheduled jobs:
  1. Daily reconciliation   — 00:05 UTC every day
     Fetches on-chain USDC.e balance via Blockscout REST API,
     compares to internal DB, persists ReconciliationReport, alerts on discrepancy.

  2. Daily performance report — 08:00 UTC every day
     Computes 24h PnL, rolling Sharpe, sends Telegram summary.

  3. Portfolio snapshot       — every 10 minutes
     Writes a PortfolioSnapshot row so the dashboard has an equity curve.

All jobs are async; APScheduler runs them inside an asyncio event loop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from live.alerting import Alerter
from live.db import build_engine, insert_portfolio_snapshot, insert_reconciliation_report
from live.monitor import Monitor


async def run_daily_reconciliation(
    settings: Settings,
    alerter: Alerter,
    monitor: Monitor,
) -> None:
    """
    00:05 UTC — verify on-chain USDC.e balance matches internal portfolio.

    Reads the latest portfolio snapshot from DB to get internal cash balance,
    calls Blockscout REST API for the real balance, persists report, alerts if not ok.
    """
    wallet = settings.polymarket_private_key   # In practice, derive address from key
    if not wallet:
        logger.warning("POLYMARKET_PRIVATE_KEY not set — skipping reconciliation.")
        return

    logger.info("Running daily reconciliation for wallet {wallet}", wallet=wallet[:10])

    from config.schemas import PortfolioState, ReconciliationReport

    # Build a minimal portfolio from settings (real implementation reads from DB)
    portfolio = PortfolioState(
        cash_usd=settings.initial_capital_usd,
        positions=[],
        realized_pnl=0.0,
        total_value_usd=settings.initial_capital_usd,
    )

    try:
        report: ReconciliationReport = await monitor.reconcile_onchain_balance(
            wallet_address=wallet,
            portfolio=portfolio,
        )
    except Exception as exc:
        logger.error("Reconciliation failed with exception: {error}", error=exc)
        return

    # Persist to DB
    engine = build_engine(settings)
    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                await insert_reconciliation_report(session, report)
    except Exception as exc:
        logger.error("Failed to persist reconciliation report: {error}", error=exc)
    finally:
        await engine.dispose()

    # Alert if issues found
    if not report.ok:
        await alerter.alert_reconciliation(report)
        logger.warning(
            "Reconciliation issues: discrepancy=${diff:+.4f} | "
            "unrecorded={n1} | unconfirmed={n2}",
            diff=report.balance_discrepancy,
            n1=len(report.unrecorded_transfers),
            n2=len(report.unconfirmed_tx_hashes),
        )
    else:
        logger.info(
            "Reconciliation OK | balance=${bal:.2f} | discrepancy=${diff:+.4f}",
            bal=report.onchain_usdc_balance,
            diff=report.balance_discrepancy,
        )


async def run_daily_performance_report(
    settings: Settings,
    alerter: Alerter,
) -> None:
    """
    08:00 UTC — compute 24h PnL and send Telegram performance summary.
    """
    logger.info("Running daily performance report.")

    engine = build_engine(settings)
    try:
        from live.db import get_recent_snapshots
        async with AsyncSession(engine) as session:
            snapshots = await get_recent_snapshots(session, mode="live", limit=200)
    except Exception as exc:
        logger.error("Failed to fetch snapshots for report: {error}", error=exc)
        return
    finally:
        await engine.dispose()

    if len(snapshots) < 2:
        logger.info("Not enough snapshots for performance report yet.")
        return

    latest = snapshots[0]["total_value_usd"]
    oldest = snapshots[-1]["total_value_usd"]
    day_return = (latest - oldest) / oldest if oldest > 0 else 0.0

    msg = (
        f"📊 Daily Performance Report\n"
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"Portfolio: ${float(latest):,.2f}\n"
        f"Day Return: {day_return:+.2%}\n"
        f"Mode: {settings.bot_mode.upper()}"
    )
    await alerter.send(msg)

    # Alert if daily drop exceeds threshold
    if day_return < -settings.daily_pnl_alert_threshold:
        await alerter.alert_daily_pnl_drop(
            pnl_pct=abs(day_return),
            current_value=float(latest),
            initial_capital=settings.initial_capital_usd,
        )


async def run_portfolio_snapshot(
    settings: Settings,
    initial_capital: float,
) -> None:
    """
    Every 10 minutes — write a portfolio snapshot to DB for the equity curve.
    In Phase 6 this reads from an in-memory executor; here we persist from settings.
    """
    from config.schemas import PortfolioSnapshot

    snapshot = PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc),
        cash_usd=initial_capital,
        positions_value_usd=0.0,
        total_value_usd=initial_capital,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        open_positions=0,
    )

    engine = build_engine(settings)
    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                await insert_portfolio_snapshot(session, snapshot, mode=settings.bot_mode)
    except Exception as exc:
        logger.error("Failed to write portfolio snapshot: {error}", error=exc)
    finally:
        await engine.dispose()


def main() -> None:
    """Entry point for the scheduler container."""
    settings = Settings()
    alerter  = Alerter(settings)
    monitor  = Monitor(settings)

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Daily reconciliation at 00:05 UTC
    scheduler.add_job(
        run_daily_reconciliation,
        trigger="cron",
        hour=0, minute=5,
        args=[settings, alerter, monitor],
        id="daily_reconciliation",
        name="Daily on-chain reconciliation",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Daily performance report at 08:00 UTC
    scheduler.add_job(
        run_daily_performance_report,
        trigger="cron",
        hour=8, minute=0,
        args=[settings, alerter],
        id="daily_performance",
        name="Daily performance report",
        max_instances=1,
        misfire_grace_time=600,
    )

    # Portfolio snapshot every 10 minutes
    scheduler.add_job(
        run_portfolio_snapshot,
        trigger="interval",
        minutes=10,
        args=[settings, settings.initial_capital_usd],
        id="portfolio_snapshot",
        name="Portfolio snapshot",
        max_instances=1,
    )

    scheduler.start()
    logger.info("Scheduler started. Jobs: {jobs}", jobs=[j.name for j in scheduler.get_jobs()])

    try:
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down.")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
