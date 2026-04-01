"""
Database persistence layer — Phase 6.

SQLAlchemy async (asyncpg) helpers for inserting and querying:
  - live_orders        ← every real order submitted to the CLOB
  - trades             ← backtest + paper + live fills (shared with earlier phases)
  - portfolio_snapshots← point-in-time equity curve
  - reconciliation_reports ← Blockscout on-chain audit results

Usage:
    engine = build_engine(settings)
    async with AsyncSession(engine) as session:
        await insert_live_order(session, fill, order_id="ord1", clob_order_id="clob_xyz")
    await engine.dispose()

Or use the module-level singleton:
    await init_db(settings)
    await insert_live_order_global(fill, ...)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config.schemas import OrderFill, PortfolioSnapshot, ReconciliationReport
from config.settings import Settings

_engine: AsyncEngine | None = None


# ── Engine factory ────────────────────────────────────────────────────────────

def build_engine(settings: Settings) -> AsyncEngine:
    """Create a new async engine. Call once at startup."""
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )


async def init_db(settings: Settings) -> None:
    """Initialize the module-level engine singleton."""
    global _engine
    _engine = build_engine(settings)


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("DB not initialised — call init_db() first")
    return _engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """
    Return an async_sessionmaker bound to *engine*.

    expire_on_commit=False prevents lazy-load errors after commit in async
    sessions (SQLAlchemy async best practice).
    """
    return async_sessionmaker(engine, expire_on_commit=False)


# ── insert_live_order ─────────────────────────────────────────────────────────

async def insert_live_order(
    session: AsyncSession,
    fill: OrderFill,
    strategy: str,
    condition_id: str,
    order_type: str = "MARKET",
    clob_order_id: Optional[str] = None,
    tx_hash: Optional[str] = None,
    limit_price: Optional[float] = None,
) -> None:
    """Persist a filled live order to the live_orders table."""
    status = "FILLED" if fill.filled_size_usd >= fill.requested_size_usd * 0.99 else "PARTIAL"
    await session.execute(
        text("""
            INSERT INTO live_orders
                (order_id, clob_order_id, tx_hash, strategy, condition_id, token_id,
                 side, order_type, requested_size_usd, filled_size_usd, limit_price,
                 fill_price, slippage_bps, fee_usd, status, submitted_at, filled_at)
            VALUES
                (:order_id, :clob_order_id, :tx_hash, :strategy, :condition_id, :token_id,
                 :side, :order_type, :requested_size_usd, :filled_size_usd, :limit_price,
                 :fill_price, :slippage_bps, :fee_usd, :status, :submitted_at, :filled_at)
            ON CONFLICT (order_id) DO UPDATE SET
                clob_order_id = EXCLUDED.clob_order_id,
                tx_hash       = EXCLUDED.tx_hash,
                filled_size_usd = EXCLUDED.filled_size_usd,
                fill_price    = EXCLUDED.fill_price,
                status        = EXCLUDED.status,
                filled_at     = EXCLUDED.filled_at
        """),
        {
            "order_id":          fill.order_id,
            "clob_order_id":     clob_order_id,
            "tx_hash":           tx_hash,
            "strategy":          strategy,
            "condition_id":      condition_id,
            "token_id":          fill.token_id,
            "side":              fill.side,
            "order_type":        order_type,
            "requested_size_usd": fill.requested_size_usd,
            "filled_size_usd":   fill.filled_size_usd,
            "limit_price":       limit_price,
            "fill_price":        fill.fill_price,
            "slippage_bps":      fill.slippage_bps,
            "fee_usd":           fill.fee_usd,
            "status":            status,
            "submitted_at":      fill.timestamp,
            "filled_at":         fill.timestamp,
        },
    )


# ── insert_trade ──────────────────────────────────────────────────────────────

async def insert_trade(
    session: AsyncSession,
    fill: OrderFill,
    strategy: str,
    condition_id: str,
    mode: str = "live",
) -> None:
    """Persist a fill to the shared trades table (backtest / paper / live)."""
    await session.execute(
        text("""
            INSERT INTO trades
                (strategy, condition_id, token_id, side, size_usd,
                 price, fee_usd, mode, executed_at)
            VALUES
                (:strategy, :condition_id, :token_id, :side, :size_usd,
                 :price, :fee_usd, :mode, :executed_at)
        """),
        {
            "strategy":     strategy,
            "condition_id": condition_id,
            "token_id":     fill.token_id,
            "side":         fill.side,
            "size_usd":     fill.filled_size_usd,
            "price":        fill.fill_price,
            "fee_usd":      fill.fee_usd,
            "mode":         mode,
            "executed_at":  fill.timestamp,
        },
    )


# ── insert_portfolio_snapshot ─────────────────────────────────────────────────

async def insert_portfolio_snapshot(
    session: AsyncSession,
    snapshot: PortfolioSnapshot,
    mode: str = "live",
) -> None:
    """Persist a portfolio snapshot to build the equity curve."""
    await session.execute(
        text("""
            INSERT INTO portfolio_snapshots
                (mode, cash_usd, positions_value_usd, total_value_usd,
                 unrealized_pnl, realized_pnl, open_positions, snapshot_at)
            VALUES
                (:mode, :cash_usd, :positions_value_usd, :total_value_usd,
                 :unrealized_pnl, :realized_pnl, :open_positions, :snapshot_at)
        """),
        {
            "mode":                mode,
            "cash_usd":            snapshot.cash_usd,
            "positions_value_usd": snapshot.positions_value_usd,
            "total_value_usd":     snapshot.total_value_usd,
            "unrealized_pnl":      snapshot.unrealized_pnl,
            "realized_pnl":        snapshot.realized_pnl,
            "open_positions":      snapshot.open_positions,
            "snapshot_at":         snapshot.timestamp or datetime.now(timezone.utc),
        },
    )


# ── insert_reconciliation_report ──────────────────────────────────────────────

async def insert_reconciliation_report(
    session: AsyncSession,
    report: ReconciliationReport,
) -> None:
    """Persist a Blockscout reconciliation report."""
    await session.execute(
        text("""
            INSERT INTO reconciliation_reports
                (wallet_address, chain_id, onchain_usdc_balance, internal_cash_balance,
                 balance_discrepancy, unrecorded_transfers, unconfirmed_tx_hashes,
                 ok, checked_at)
            VALUES
                (:wallet_address, :chain_id, :onchain_usdc_balance, :internal_cash_balance,
                 :balance_discrepancy, :unrecorded_transfers::jsonb, :unconfirmed_tx_hashes::jsonb,
                 :ok, :checked_at)
        """),
        {
            "wallet_address":        report.wallet_address,
            "chain_id":              report.chain_id,
            "onchain_usdc_balance":  report.onchain_usdc_balance,
            "internal_cash_balance": report.internal_cash_balance,
            "balance_discrepancy":   report.balance_discrepancy,
            "unrecorded_transfers":  json.dumps(report.unrecorded_transfers),
            "unconfirmed_tx_hashes": json.dumps(report.unconfirmed_tx_hashes),
            "ok":                    report.ok,
            "checked_at":            report.checked_at,
        },
    )


# ── queries ───────────────────────────────────────────────────────────────────

async def get_open_live_orders(session: AsyncSession) -> list[dict]:
    """Return all live orders still in PENDING/PARTIAL status."""
    result = await session.execute(
        text("""
            SELECT order_id, clob_order_id, tx_hash, token_id, side,
                   filled_size_usd, fill_price, submitted_at
            FROM live_orders
            WHERE status IN ('PENDING', 'PARTIAL')
            ORDER BY submitted_at DESC
        """)
    )
    return [dict(row._mapping) for row in result]


async def get_recent_snapshots(
    session: AsyncSession,
    mode: str = "live",
    limit: int = 200,
) -> list[dict]:
    """Return recent portfolio snapshots ordered newest-first."""
    result = await session.execute(
        text("""
            SELECT total_value_usd, realized_pnl, snapshot_at
            FROM portfolio_snapshots
            WHERE mode = :mode
            ORDER BY snapshot_at DESC
            LIMIT :limit
        """),
        {"mode": mode, "limit": limit},
    )
    return [dict(row._mapping) for row in result]
