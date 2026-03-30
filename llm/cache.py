"""
SQLite-backed LLM response cache with TTL.

Keyed by (condition_id, prompt_hash). Prevents re-querying the same market
within LLM_CACHE_TTL_HOURS. Also tracks estimated daily API spend.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

from loguru import logger

from config.schemas import LLMEstimate
from config.settings import settings

_DEFAULT_DB_PATH = Path("data/llm_cache.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    condition_id        TEXT NOT NULL,
    prompt_hash         TEXT NOT NULL,
    model               TEXT NOT NULL,
    probability         REAL NOT NULL,
    confidence          REAL,
    reasoning           TEXT,
    sources             TEXT,
    estimated_cost_usd  REAL DEFAULT 0.0,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (condition_id, prompt_hash)
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_created_at ON llm_cache (created_at);
"""


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class LLMCache:
    """SQLite cache for LLM probability estimates.

    Thread-safe: each call opens/closes its own connection.
    """

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_CREATE_TABLE_SQL)

    def get(self, condition_id: str, prompt_hash: str) -> LLMEstimate | None:
        """Return a cached estimate if it exists and is within TTL.

        Returns None if not found or if the cache entry is expired.
        """
        ttl_cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=settings.llm_cache_ttl_hours)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT condition_id, model, prompt_hash, probability, confidence,
                       reasoning, sources, created_at
                FROM llm_cache
                WHERE condition_id = ? AND prompt_hash = ? AND created_at >= ?
                """,
                (condition_id, prompt_hash, _to_iso(ttl_cutoff)),
            ).fetchone()

        if row is None:
            return None

        sources = json.loads(row["sources"]) if row["sources"] else None
        return LLMEstimate(
            condition_id=row["condition_id"],
            model=row["model"],
            prompt_hash=row["prompt_hash"],
            probability=row["probability"],
            confidence=row["confidence"],
            reasoning=row["reasoning"],
            sources=sources,
        )

    def save(self, estimate: LLMEstimate, estimated_cost_usd: float = 0.0) -> None:
        """Upsert an LLMEstimate into the cache."""
        now = _to_iso(datetime.now(tz=timezone.utc))
        sources_json = json.dumps(estimate.sources) if estimate.sources else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_cache
                    (condition_id, prompt_hash, model, probability, confidence,
                     reasoning, sources, estimated_cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    estimate.condition_id,
                    estimate.prompt_hash,
                    estimate.model,
                    estimate.probability,
                    estimate.confidence,
                    estimate.reasoning,
                    sources_json,
                    estimated_cost_usd,
                    now,
                ),
            )

        logger.debug(
            "LLMCache: saved estimate",
            condition_id=estimate.condition_id,
            probability=estimate.probability,
            cost=estimated_cost_usd,
        )

    def get_daily_cost(self) -> float:
        """Return the total estimated API cost for today (UTC)."""
        today_start = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) FROM llm_cache WHERE created_at >= ?",
                (_to_iso(today_start),),
            ).fetchone()
        return float(row[0])

    def expire_old(self, ttl_hours: int | None = None) -> int:
        """Delete cache entries older than ttl_hours. Returns rows deleted."""
        hours = ttl_hours or settings.llm_cache_ttl_hours
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM llm_cache WHERE created_at < ?",
                (_to_iso(cutoff),),
            )
            return cursor.rowcount

    # Legacy interface (set/get with dict)
    def set(self, prompt_hash: str, response: dict, ttl_seconds: int = 3600) -> None:
        """Legacy dict-based setter — wraps save() for backward compatibility."""
        estimate = LLMEstimate(
            condition_id=response.get("condition_id", ""),
            model=response.get("model", "unknown"),
            prompt_hash=prompt_hash,
            probability=response["probability"],
            confidence=response.get("confidence"),
            reasoning=response.get("reasoning"),
            sources=response.get("sources"),
        )
        self.save(estimate)
