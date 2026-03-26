"""
PostgreSQL-backed article cache with TTL.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class NewsStore:
    def save(self, article: object) -> None:
        raise NotImplementedError("NewsStore will be implemented in Phase 4")

    def get_recent(self, hours: int = 24) -> list:
        raise NotImplementedError("NewsStore will be implemented in Phase 4")
