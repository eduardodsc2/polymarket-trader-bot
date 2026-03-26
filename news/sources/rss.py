"""
Generic RSS feed parser (Reuters, AP, CoinDesk, BBC…).

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import NewsArticle


class RSSSource:
    def __init__(self, feed_url: str) -> None:
        self.feed_url = feed_url

    def fetch(self) -> list[NewsArticle]:
        raise NotImplementedError("RSSSource will be implemented in Phase 4")
