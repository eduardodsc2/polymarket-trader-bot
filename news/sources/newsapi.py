"""
NewsAPI.org wrapper (free tier: 100 req/day).

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import NewsArticle


class NewsAPISource:
    def fetch(self, query: str, page_size: int = 20) -> list[NewsArticle]:
        raise NotImplementedError("NewsAPISource will be implemented in Phase 4")
