"""
News pipeline orchestrator.

Collects articles from RSS, NewsAPI, Reddit, and LunarCrush (for crypto).

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import NewsArticle


class NewsFetcher:
    def fetch_for_market(self, question: str, category: str) -> list[NewsArticle]:
        raise NotImplementedError("NewsFetcher will be implemented in Phase 4")
