"""
SQLite-backed article cache with TTL.

Stores NewsArticle objects keyed by URL to avoid duplicates.
The get_articles() method enforces strict timestamp filtering to prevent
lookahead bias: only articles with published_at < before are returned.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

from loguru import logger

from config.schemas import NewsArticle

_DEFAULT_DB_PATH = Path("data/news_cache.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    url          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT,
    published_at TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    relevance_score REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles (published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source);
"""


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class NewsStore:
    """SQLite-backed store for NewsArticle objects with TTL-based expiry.

    Thread-safe: each call opens and closes its own connection.
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

    def save(self, articles: list[NewsArticle]) -> int:
        """Upsert articles into the store. Deduplicates by URL.

        Returns the number of new articles inserted (not updated).
        """
        if not articles:
            return 0

        rows = [
            (
                art.url or f"no-url:{art.source}:{art.title[:64]}",
                art.source,
                art.title,
                art.body,
                _to_iso(art.published_at),
                _to_iso(art.fetched_at or datetime.now(tz=timezone.utc)),
                art.relevance_score,
            )
            for art in articles
        ]

        inserted = 0
        with self._connect() as conn:
            for row in rows:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO articles
                        (url, source, title, body, published_at, fetched_at, relevance_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += cursor.rowcount

        logger.debug("NewsStore: saved articles", total=len(rows), new=inserted)
        return inserted

    def get_articles(
        self,
        keywords: list[str] | None = None,
        before: datetime | None = None,
        lookback_hours: int = 48,
        limit: int = 200,
    ) -> list[NewsArticle]:
        """Retrieve articles, filtered and sorted by published_at descending.

        ANTI-LOOKAHEAD RULE: Only articles with published_at < before are returned.
        This is enforced strictly — never return articles after the decision timestamp.

        Args:
            keywords: Optional list of keywords to filter by (OR logic, checks title).
            before: Upper bound for published_at (exclusive). Defaults to now.
            lookback_hours: How many hours back to search.
            limit: Maximum number of articles to return.
        """
        cutoff = (before or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        earliest = cutoff - timedelta(hours=lookback_hours)

        sql = """
            SELECT url, source, title, body, published_at, fetched_at, relevance_score
            FROM articles
            WHERE published_at < ?
              AND published_at >= ?
            ORDER BY published_at DESC
            LIMIT ?
        """
        params: list = [_to_iso(cutoff), _to_iso(earliest), limit]

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        articles = [
            NewsArticle(
                source=row["source"],
                title=row["title"],
                body=row["body"],
                url=row["url"],
                published_at=_from_iso(row["published_at"]),
                fetched_at=_from_iso(row["fetched_at"]),
                relevance_score=row["relevance_score"] or 0.0,
            )
            for row in rows
        ]

        # Client-side keyword filter (simple contains check)
        if keywords:
            kw_lower = [kw.lower() for kw in keywords]
            articles = [
                a
                for a in articles
                if any(kw in a.title.lower() or kw in (a.body or "").lower() for kw in kw_lower)
            ]

        return articles

    def expire_old(self, ttl_hours: int = 72) -> int:
        """Delete articles older than ttl_hours. Returns number of rows deleted."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=ttl_hours)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM articles WHERE fetched_at < ?",
                (_to_iso(cutoff),),
            )
            deleted = cursor.rowcount

        if deleted:
            logger.debug("NewsStore: expired old articles", count=deleted)
        return deleted

    def count(self) -> int:
        """Return total number of articles in the store."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
