"""SQLAlchemy Core table definitions.

These mirror the schema in db/init.sql exactly.
Used by repository.py for type-safe upserts.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    MetaData,
    Numeric,
    Table,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

markets_table = Table(
    "markets",
    metadata,
    Column("condition_id", Text, primary_key=True),
    Column("question", Text, nullable=False),
    Column("category", Text),
    Column("end_date", TIMESTAMP(timezone=True)),
    Column("resolved", Boolean, default=False),
    Column("outcome", Text),
    Column("volume_usd", Numeric),
    Column("liquidity_usd", Numeric),
    Column("fetched_at", TIMESTAMP(timezone=True)),
)

prices_table = Table(
    "prices",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("token_id", Text, nullable=False),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("price", Numeric, nullable=False),
    Column("volume", Numeric),
)

orderbook_snapshots_table = Table(
    "orderbook_snapshots",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("token_id", Text, nullable=False),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("bids", JSONB),
    Column("asks", JSONB),
    Column("mid_price", Numeric),
    Column("spread", Numeric),
    UniqueConstraint("token_id", "timestamp", name="uq_orderbook_token_ts"),
)
