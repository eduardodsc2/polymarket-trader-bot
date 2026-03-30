"""
Sentiment scoring using VADER (local, no API required).

All functions are pure — no I/O, no side effects. Receives a list of
NewsArticle objects and returns a NewsFeatures object with numeric signals.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from config.schemas import NewsArticle, NewsFeatures


@lru_cache(maxsize=1)
def _get_analyzer():
    """Lazy-load the VADER SentimentIntensityAnalyzer (singleton)."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    return SentimentIntensityAnalyzer()


def score_text(text: str) -> float:
    """Return VADER compound sentiment score for a text string.

    Returns a float in [-1, +1]:
        +1 = maximally positive
         0 = neutral
        -1 = maximally negative
    """
    if not text or not text.strip():
        return 0.0
    analyzer = _get_analyzer()
    return analyzer.polarity_scores(text)["compound"]


def compute_sentiment(
    articles: list[NewsArticle],
    condition_id: str,
    timestamp: datetime | None = None,
) -> NewsFeatures:
    """Compute NewsFeatures from a list of articles for a given market.

    Pure function — same inputs always produce the same output.

    Args:
        articles: Articles to compute features from (may be empty).
        condition_id: Market condition ID (stored in the features record).
        timestamp: Decision timestamp (defaults to now UTC).

    Returns:
        NewsFeatures with all numeric signals populated.
    """
    ts = timestamp or datetime.now(tz=timezone.utc)

    if not articles:
        return NewsFeatures(
            condition_id=condition_id,
            timestamp=ts,
            article_count_24h=0,
            article_count_delta=0.0,
            avg_sentiment_score=0.0,
            sentiment_std=0.0,
            sentiment_delta_24h=0.0,
            price_vs_sentiment_gap=None,
        )

    cutoff_24h = ts - timedelta(hours=24)
    cutoff_48h = ts - timedelta(hours=48)

    # Split into 0–24h and 24–48h windows
    recent: list[NewsArticle] = []
    prior: list[NewsArticle] = []
    for art in articles:
        pub = art.published_at.replace(tzinfo=timezone.utc) if art.published_at.tzinfo is None else art.published_at
        if pub >= cutoff_24h:
            recent.append(art)
        elif pub >= cutoff_48h:
            prior.append(art)

    article_count_24h = len(recent)
    article_count_delta = float(len(recent) - len(prior))

    # VADER scores for recent articles
    scores = [
        score_text((art.title or "") + " " + (art.body or ""))
        for art in recent
    ]

    if not scores:
        # Fall back to all articles if no recent ones
        scores = [
            score_text((art.title or "") + " " + (art.body or ""))
            for art in articles
        ]

    avg_sentiment = statistics.mean(scores) if scores else 0.0
    sentiment_std = statistics.stdev(scores) if len(scores) > 1 else 0.0

    # Sentiment trend: recent avg vs. prior avg
    prior_scores = [
        score_text((art.title or "") + " " + (art.body or ""))
        for art in prior
    ]
    prior_avg = statistics.mean(prior_scores) if prior_scores else avg_sentiment
    sentiment_delta_24h = avg_sentiment - prior_avg

    return NewsFeatures(
        condition_id=condition_id,
        timestamp=ts,
        article_count_24h=article_count_24h,
        article_count_delta=article_count_delta,
        avg_sentiment_score=round(avg_sentiment, 4),
        sentiment_std=round(sentiment_std, 4),
        sentiment_delta_24h=round(sentiment_delta_24h, 4),
        price_vs_sentiment_gap=None,  # filled by caller with market price context
    )


class SentimentScorer:
    """Stateless wrapper around compute_sentiment for DI compatibility."""

    def score(self, text: str) -> float:
        """Return VADER compound sentiment score in [-1, +1]."""
        return score_text(text)

    def compute_features(
        self,
        articles: list[NewsArticle],
        condition_id: str,
        timestamp: datetime | None = None,
    ) -> NewsFeatures:
        """Compute full NewsFeatures from article list."""
        return compute_sentiment(articles, condition_id, timestamp)
