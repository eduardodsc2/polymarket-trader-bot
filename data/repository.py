"""Async DB upsert operations for all data models.

All functions accept an injected AsyncSession — no sessions are created here.
Call these from scripts via asyncio.run(), or from async bot code directly.

Layer contract: receives Pydantic models, writes to DB, returns row counts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.schemas import Market, OrderbookSnapshot, PricePoint
from data.models import markets_table, orderbook_snapshots_table, prices_table


async def upsert_markets(session: AsyncSession, markets: Sequence[Market]) -> int:
    """Upsert markets by condition_id. Returns number of rows upserted."""
    if not markets:
        return 0

    rows = [
        {
            "condition_id": m.condition_id,
            "question": m.question,
            "category": m.category,
            "end_date": m.end_date,
            "resolved": m.resolved,
            "outcome": m.outcome,
            "volume_usd": m.volume_usd,
            "liquidity_usd": m.liquidity_usd,
            "fetched_at": m.fetched_at or datetime.now(timezone.utc),
        }
        for m in markets
    ]

    stmt = pg_insert(markets_table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["condition_id"],
        set_={
            "question": stmt.excluded.question,
            "category": stmt.excluded.category,
            "end_date": stmt.excluded.end_date,
            "resolved": stmt.excluded.resolved,
            "outcome": stmt.excluded.outcome,
            "volume_usd": stmt.excluded.volume_usd,
            "liquidity_usd": stmt.excluded.liquidity_usd,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )

    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def upsert_prices(session: AsyncSession, prices: Sequence[PricePoint]) -> int:
    """Upsert price points by (token_id, timestamp). Returns rows upserted."""
    if not prices:
        return 0

    rows = [
        {
            "token_id": p.token_id,
            "timestamp": p.timestamp,
            "price": p.price,
            "volume": p.volume,
        }
        for p in prices
    ]

    stmt = pg_insert(prices_table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["token_id", "timestamp"],
        set_={
            "price": stmt.excluded.price,
            "volume": stmt.excluded.volume,
        },
    )

    await session.execute(stmt)
    await session.commit()
    return len(rows)


async def upsert_orderbook_snapshot(
    session: AsyncSession,
    snapshot: OrderbookSnapshot,
) -> None:
    """Upsert a single orderbook snapshot by (token_id, timestamp)."""
    row = {
        "token_id": snapshot.token_id,
        "timestamp": snapshot.timestamp,
        "bids": [{"price": b.price, "size": b.size} for b in snapshot.bids],
        "asks": [{"price": a.price, "size": a.size} for a in snapshot.asks],
        "mid_price": snapshot.mid_price,
        "spread": snapshot.spread,
    }
    stmt = pg_insert(orderbook_snapshots_table).values(**row)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_orderbook_token_ts",
        set_={
            "bids": stmt.excluded.bids,
            "asks": stmt.excluded.asks,
            "mid_price": stmt.excluded.mid_price,
            "spread": stmt.excluded.spread,
        },
    )
    await session.execute(stmt)
    await session.commit()
