"""
News pipeline orchestrator.

Collects articles from RSS, NewsAPI, Reddit, and LunarCrush (for crypto).
Applies relevance filtering, deduplicates via the store, and returns the
top-N most relevant articles for a given market question.
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from config.schemas import NewsArticle, NewsFeatures
from config.settings import settings
from news.relevance import RelevanceScorer, extract_keywords
from news.sentiment import compute_sentiment
from news.sources.lunarcrush import LunarCrushSource, is_applicable, extract_topic
from news.sources.newsapi import NewsAPISource
from news.sources.reddit import RedditSource
from news.sources.rss import RSSSource, build_default_rss_sources
from news.store import NewsStore


class NewsFetcher:
    """Orchestrates all news sources for a given market question.

    All dependencies are injected via constructor (DI rule).
    In production: pass real source instances.
    In tests: pass mocks or empty lists.
    """

    def __init__(
        self,
        rss_sources: list[RSSSource] | None = None,
        newsapi: NewsAPISource | None = None,
        reddit: RedditSource | None = None,
        lunarcrush: LunarCrushSource | None = None,
        store: NewsStore | None = None,
        scorer: RelevanceScorer | None = None,
    ) -> None:
        self._rss_sources = rss_sources if rss_sources is not None else build_default_rss_sources()
        self._newsapi = newsapi or NewsAPISource()
        self._reddit = reddit or RedditSource()
        self._lunarcrush = lunarcrush or LunarCrushSource()
        self._store = store or NewsStore()
        self._scorer = scorer or RelevanceScorer()

    # ── Public API ──────────────────────────────────────────────────────────

    def fetch_for_market(
        self,
        question: str,
        category: str,
        lookback_hours: int | None = None,
        max_articles: int | None = None,
    ) -> list[NewsArticle]:
        """Fetch and rank articles relevant to a market question.

        Pipeline:
        1. Pull fresh articles from all configured sources.
        2. Save all to the store (dedup by URL).
        3. Re-query store with anti-lookahead cutoff = now.
        4. Score by relevance and return top-N.

        Args:
            question: The market question text.
            category: Market category (e.g. "crypto", "politics").
            lookback_hours: Override NEWS_LOOKBACK_HOURS from settings.
            max_articles: Override NEWS_MAX_ARTICLES_PER_PROMPT from settings.

        Returns:
            List of NewsArticle objects ranked by relevance_score descending.
        """
        lookback = lookback_hours or settings.news_lookback_hours
        cap = max_articles or settings.news_max_articles_per_prompt
        keywords = extract_keywords(question)

        # 1. Collect from all sources
        raw: list[NewsArticle] = []
        raw.extend(self._fetch_rss())
        raw.extend(self._fetch_newsapi(keywords, lookback))
        raw.extend(self._fetch_reddit(category))

        logger.info(
            "NewsFetcher: raw articles collected",
            question=question[:60],
            total=len(raw),
        )

        # 2. Persist to store (dedup by URL)
        self._store.save(raw)

        # 3. Re-query store with strict before=now
        stored = self._store.get_articles(
            keywords=keywords,
            before=datetime.now(tz=timezone.utc),
            lookback_hours=lookback,
        )

        # 4. Rank by relevance
        ranked = self._scorer.rank(question, stored)

        logger.info(
            "NewsFetcher: articles ranked and filtered",
            question=question[:60],
            ranked=len(ranked),
            returning=min(cap, len(ranked)),
        )
        return ranked[:cap]

    def fetch_for_market_at(
        self,
        question: str,
        category: str,
        before: datetime,
        lookback_hours: int | None = None,
        max_articles: int | None = None,
    ) -> list[NewsArticle]:
        """Backtest-safe variant: returns only articles published before `before`.

        Used by the backtest engine to prevent lookahead bias.
        Does NOT fetch new articles — only queries the existing store.
        """
        lookback = lookback_hours or settings.news_lookback_hours
        cap = max_articles or settings.news_max_articles_per_prompt
        keywords = extract_keywords(question)

        stored = self._store.get_articles(
            keywords=keywords,
            before=before,
            lookback_hours=lookback,
        )

        ranked = self._scorer.rank(question, stored)
        return ranked[:cap]

    def compute_features(
        self,
        question: str,
        category: str,
        condition_id: str,
        timestamp: datetime | None = None,
    ) -> NewsFeatures:
        """Fetch articles and compute NewsFeatures for a market.

        Combines news volume + VADER sentiment signals.
        """
        ts = timestamp or datetime.now(tz=timezone.utc)
        keywords = extract_keywords(question)

        articles = self._store.get_articles(
            keywords=keywords,
            before=ts,
            lookback_hours=settings.news_lookback_hours,
        )

        return compute_sentiment(articles, condition_id, ts)

    # ── Private helpers ─────────────────────────────────────────────────────

    def _fetch_rss(self) -> list[NewsArticle]:
        articles: list[NewsArticle] = []
        for source in self._rss_sources:
            try:
                articles.extend(source.fetch())
            except Exception as exc:
                logger.error("RSS source failed", source=source.source_name, error=str(exc))
        return articles

    def _fetch_newsapi(self, keywords: list[str], lookback_hours: int) -> list[NewsArticle]:
        if not keywords:
            return []
        try:
            return self._newsapi.fetch(keywords=keywords, lookback_hours=lookback_hours)
        except Exception as exc:
            logger.error("NewsAPI fetch failed", error=str(exc))
            return []

    def _fetch_reddit(self, category: str) -> list[NewsArticle]:
        try:
            return self._reddit.fetch_for_category(category)
        except Exception as exc:
            logger.error("Reddit fetch failed", category=category, error=str(exc))
            return []
