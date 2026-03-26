"""
Reddit PRAW wrapper.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import NewsArticle


class RedditSource:
    def fetch(self, subreddit: str, limit: int = 25) -> list[NewsArticle]:
        raise NotImplementedError("RedditSource will be implemented in Phase 4")
