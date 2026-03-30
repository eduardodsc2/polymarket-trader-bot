"""
Reddit PRAW wrapper.

Fetches hot/new posts from specified subreddits and normalizes them into
NewsArticle objects. Operates in read-only mode (no user login required).
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from config.schemas import NewsArticle
from config.settings import settings

_BODY_MAX_CHARS = 500

# Default subreddits mapped to market categories
CATEGORY_SUBREDDITS: dict[str, list[str]] = {
    "politics":  ["politics", "worldnews"],
    "crypto":    ["CryptoCurrency", "Bitcoin", "ethereum"],
    "sports":    ["sports", "soccer", "nfl"],
    "economics": ["Economics", "worldnews"],
    "default":   ["worldnews", "news"],
}


def _truncate(text: str) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    return cleaned[:_BODY_MAX_CHARS] + "…" if len(cleaned) > _BODY_MAX_CHARS else cleaned or None


class RedditSource:
    """Fetches posts from Reddit via PRAW in read-only mode."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        user_agent: str = "",
    ) -> None:
        self._client_id = client_id or settings.reddit_client_id
        self._client_secret = client_secret or settings.reddit_client_secret
        self._user_agent = user_agent or settings.reddit_user_agent
        self._reddit = None  # lazy init

    def _get_reddit(self):
        """Lazily initialize the PRAW Reddit instance."""
        if self._reddit is None:
            import praw

            self._reddit = praw.Reddit(
                client_id=self._client_id,
                client_secret=self._client_secret,
                user_agent=self._user_agent,
                ratelimit_seconds=300,
            )
            self._reddit.read_only = True
        return self._reddit

    def fetch(self, subreddit: str, limit: int = 25) -> list[NewsArticle]:
        """Fetch hot posts from a subreddit.

        Args:
            subreddit: Subreddit name without r/ prefix (e.g. "worldnews").
            limit: Maximum number of posts to fetch (max 100).

        Returns:
            List of NewsArticle objects with valid timestamps.
            Returns empty list if credentials are missing or request fails.
        """
        if not self._client_id or not self._client_secret:
            logger.warning("Reddit credentials not configured — skipping Reddit fetch")
            return []

        try:
            import praw.exceptions

            reddit = self._get_reddit()
            now = datetime.now(tz=timezone.utc)
            articles: list[NewsArticle] = []

            for submission in reddit.subreddit(subreddit).hot(limit=limit):
                pub_dt = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)

                title = (submission.title or "").strip()
                if not title:
                    continue

                body = _truncate(submission.selftext) if submission.is_self else None
                url = f"https://reddit.com{submission.permalink}"

                articles.append(
                    NewsArticle(
                        source=f"reddit:r/{subreddit}",
                        title=title,
                        body=body,
                        url=url,
                        published_at=pub_dt,
                        fetched_at=now,
                    )
                )

            logger.debug(
                "Reddit fetch complete",
                subreddit=subreddit,
                kept=len(articles),
            )
            return articles

        except Exception as exc:
            logger.error(
                "Reddit fetch failed",
                subreddit=subreddit,
                error=str(exc),
            )
            return []

    def fetch_for_category(self, category: str, limit: int = 25) -> list[NewsArticle]:
        """Fetch posts from all subreddits mapped to a market category."""
        subreddits = CATEGORY_SUBREDDITS.get(category, CATEGORY_SUBREDDITS["default"])
        articles: list[NewsArticle] = []
        for sub in subreddits:
            articles.extend(self.fetch(sub, limit=limit))
        return articles
