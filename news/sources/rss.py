"""
Generic RSS feed parser (Reuters, AP, CoinDesk, BBC, Politico, …).

Uses feedparser to fetch and normalize entries into NewsArticle objects.
Entries without a parseable timestamp are discarded (anti-lookahead rule).
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import ClassVar

import feedparser
from loguru import logger

from config.schemas import NewsArticle

# Registered feeds: (source_name, url)
DEFAULT_FEEDS: list[tuple[str, str]] = [
    ("reuters",  "https://feeds.reuters.com/reuters/topNews"),
    ("ap",       "https://rsshub.app/apnews/topics/apf-topnews"),
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("politico", "https://rss.politico.com/politics-news.xml"),
    ("bbc",      "http://feeds.bbci.co.uk/news/rss.xml"),
]

_BODY_MAX_CHARS: int = 500


def _parse_timestamp(entry: feedparser.FeedParserDict) -> datetime | None:
    """Return a UTC-aware datetime from an entry, or None if unavailable."""
    raw = entry.get("published_parsed") or entry.get("updated_parsed")
    if raw is None:
        return None
    try:
        # feedparser published_parsed is always UTC (9-tuple)
        return datetime(*raw[:6], tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _clean_body(text: str | None) -> str | None:
    """Strip HTML tags and truncate to _BODY_MAX_CHARS."""
    if not text:
        return None
    # feedparser sanitizes HTML in .summary; strip remaining tags naively
    import re
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned).strip()
    if len(cleaned) > _BODY_MAX_CHARS:
        cleaned = cleaned[:_BODY_MAX_CHARS] + "…"
    return cleaned or None


class RSSSource:
    """Fetches articles from a single RSS feed URL."""

    def __init__(self, source_name: str, feed_url: str) -> None:
        self.source_name = source_name
        self.feed_url = feed_url

    def fetch(self) -> list[NewsArticle]:
        """Parse the RSS feed and return a list of NewsArticle objects.

        Entries without a valid timestamp are silently dropped — a timestamp
        is required to enforce the anti-lookahead rule in the backtest engine.
        """
        try:
            parsed = feedparser.parse(self.feed_url)
        except Exception as exc:
            logger.error(
                "RSS fetch failed",
                source=self.source_name,
                url=self.feed_url,
                error=str(exc),
            )
            return []

        if parsed.bozo:
            # bozo=1 means malformed feed — log but continue (data is still usable)
            logger.warning(
                "Bozo RSS feed (malformed but parseable)",
                source=self.source_name,
                exception=str(parsed.bozo_exception),
            )

        now = datetime.now(tz=timezone.utc)
        articles: list[NewsArticle] = []

        for entry in parsed.entries:
            pub_dt = _parse_timestamp(entry)
            if pub_dt is None:
                logger.debug(
                    "RSS entry skipped — no timestamp",
                    source=self.source_name,
                    title=getattr(entry, "title", "<no title>"),
                )
                continue

            title = getattr(entry, "title", None)
            if not title:
                continue

            articles.append(
                NewsArticle(
                    source=self.source_name,
                    title=title.strip(),
                    body=_clean_body(getattr(entry, "summary", None)),
                    url=getattr(entry, "link", None),
                    published_at=pub_dt,
                    fetched_at=now,
                )
            )

        logger.debug(
            "RSS fetch complete",
            source=self.source_name,
            total=len(parsed.entries),
            kept=len(articles),
        )
        return articles


def build_default_rss_sources() -> list[RSSSource]:
    """Return RSSSource instances for all default feeds."""
    return [RSSSource(name, url) for name, url in DEFAULT_FEEDS]
