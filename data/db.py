"""SQLAlchemy async engine and session factory.

Imported once at startup; never call os.getenv() here — use settings injection.

Usage:
    from data.db import AsyncSessionFactory

    async with AsyncSessionFactory() as session:
        await session.execute(...)
        await session.commit()
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

AsyncSessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
