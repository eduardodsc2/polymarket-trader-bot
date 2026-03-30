"""
NewsAPI.org wrapper (free tier: 100 req/day).

Searches for articles by keyword list using the /v2/everything endpoint.
Requires NEWSAPI_KEY in settings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from loguru import logger

from config.schemas import NewsArticle
from config.settings import settings

_BASE_URL = "https://newsapi.org/v2/everything"
_BODY_MAX_CHARS = 500
_REQUEST_TIMEOUT = 10  # seconds


def _truncate(text: str | None) -> str | None:
    if not text:
        return None
    return text[:_BODY_MAX_CHARS] + "…" if len(text) > _BODY_MAX_CHARS else text


def _parse_published_at(raw: str | None) -> datetime | None:
    """Parse ISO 8601 date string from NewsAPI into a UTC-aware datetime."""
    if not raw:
        return None
    try:
        # NewsAPI format: "2024-03-15T10:30:00Z"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class NewsAPISource:
    """Fetches articles from NewsAPI.org by keyword query."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key or settings.newsapi_key

    def fetch(
        self,
        keywords: list[str],
        lookback_hours: int = 48,
        max_results: int = 20,
    ) -> list[NewsArticle]:
        """Query NewsAPI /v2/everything for articles matching any keyword.

        Args:
            keywords: List of search terms (ORed together in the query).
            lookback_hours: Only fetch articles published in the last N hours.
            max_results: Maximum number of articles to return.

        Returns:
            List of NewsArticle objects with valid timestamps.
            Returns empty list if API key is missing or request fails.
        """
        if not self._api_key:
            logger.warning("NewsAPI key not configured — skipping NewsAPI fetch")
            return []

        if not keywords:
            return []

        query = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in keywords[:5])
        from_dt = (datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        params = {
            "q": query,
            "from": from_dt,
            "sortBy": "publishedAt",
            "pageSize": min(max_results, 100),
            "language": "en",
        }

        try:
            resp = requests.get(
                _BASE_URL,
                params=params,
                headers={"X-Api-Key": self._api_key},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("NewsAPI request failed", error=str(exc), query=query)
            return []

        data = resp.json()
        if data.get("status") != "ok":
            logger.error(
                "NewsAPI returned non-ok status",
                status=data.get("status"),
                code=data.get("code"),
                message=data.get("message"),
            )
            return []

        now = datetime.now(tz=timezone.utc)
        articles: list[NewsArticle] = []

        for item in data.get("articles", []):
            pub_dt = _parse_published_at(item.get("publishedAt"))
            if pub_dt is None:
                continue

            title = (item.get("title") or "").strip()
            if not title or title == "[Removed]":
                continue

            source_name = (item.get("source") or {}).get("name") or "newsapi"
            body_raw = item.get("description") or item.get("content")

            articles.append(
                NewsArticle(
                    source=f"newsapi:{source_name}",
                    title=title,
                    body=_truncate(body_raw),
                    url=item.get("url"),
                    published_at=pub_dt,
                    fetched_at=now,
                )
            )

        logger.debug(
            "NewsAPI fetch complete",
            query=query,
            total=data.get("totalResults", 0),
            kept=len(articles),
        )
        return articles
