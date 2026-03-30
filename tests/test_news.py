"""
Unit tests for Phase 4 news pipeline.

Coverage:
  - news.relevance   — extract_keywords, keyword_match_score,
                       filter_by_keywords, RelevanceScorer.rank
  - news.store       — save, get_articles (anti-lookahead), expire_old, count
  - news.sentiment   — score_text, compute_sentiment (empty + non-empty)
  - news.sources.lunarcrush — is_applicable, extract_topic,
                               normalize_topic_response,
                               normalize_time_series_response,
                               LunarCrushSource.inject_sentiment / get_cached

No network calls. NewsStore uses a tmp_path SQLite file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.schemas import NewsArticle, SentimentReading
from news.relevance import (
    RelevanceScorer,
    extract_keywords,
    filter_by_keywords,
    keyword_match_score,
)
from news.sentiment import compute_sentiment, score_text
from news.sources.lunarcrush import (
    LunarCrushSource,
    extract_topic,
    is_applicable,
    normalize_time_series_response,
    normalize_topic_response,
)
from news.store import NewsStore


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ts(hours_ago: float = 0.0) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)


def _article(
    title: str,
    body: str = "",
    source: str = "test",
    hours_ago: float = 1.0,
    url: str | None = None,
) -> NewsArticle:
    return NewsArticle(
        source=source,
        title=title,
        body=body or None,
        url=url or f"https://test.example/{title[:20].replace(' ', '-')}",
        published_at=_ts(hours_ago),
        fetched_at=_ts(0),
    )


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(db_path=tmp_path / "test_news.db")


# ── extract_keywords ──────────────────────────────────────────────────────────


class TestExtractKeywords:
    def test_removes_stopwords(self):
        kws = extract_keywords("Will the Fed raise interest rates?")
        assert "the" not in kws
        assert "will" not in kws

    def test_removes_short_tokens(self):
        kws = extract_keywords("Is AI going to be big in 2025?")
        assert "is" not in kws
        assert "to" not in kws
        assert "be" not in kws

    def test_keeps_meaningful_tokens(self):
        kws = extract_keywords("Will Bitcoin exceed $120k by end of Q2 2025?")
        assert "bitcoin" in kws
        assert "exceed" in kws
        assert "120k" in kws

    def test_deduplicates(self):
        kws = extract_keywords("bitcoin bitcoin bitcoin price")
        assert kws.count("bitcoin") == 1

    def test_empty_question(self):
        assert extract_keywords("") == []

    def test_lowercase(self):
        kws = extract_keywords("Ethereum ETH price")
        assert "ethereum" in kws
        assert "eth" in kws


# ── keyword_match_score ───────────────────────────────────────────────────────


class TestKeywordMatchScore:
    def test_full_match(self):
        art = _article("Bitcoin price hits new high", "BTC ETH crypto")
        score = keyword_match_score(art, ["bitcoin", "price", "high"])
        assert score == 1.0

    def test_partial_match(self):
        art = _article("Bitcoin rally continues")
        score = keyword_match_score(art, ["bitcoin", "ethereum"])
        assert score == 0.5

    def test_no_match(self):
        art = _article("Football scores and results")
        score = keyword_match_score(art, ["bitcoin", "crypto"])
        assert score == 0.0

    def test_empty_keywords(self):
        art = _article("Something")
        assert keyword_match_score(art, []) == 0.0

    def test_body_is_searched(self):
        art = _article("Breaking news", body="ethereum reaches new highs")
        score = keyword_match_score(art, ["ethereum"])
        assert score == 1.0


# ── filter_by_keywords ────────────────────────────────────────────────────────


class TestFilterByKeywords:
    def test_sorted_by_score(self):
        arts = [
            _article("Partial match bitcoin only"),
            _article("Full match bitcoin ethereum crypto"),
        ]
        result = filter_by_keywords(arts, ["bitcoin", "ethereum", "crypto"])
        assert result[0].title == "Full match bitcoin ethereum crypto"

    def test_zero_score_excluded(self):
        arts = [
            _article("Football scores"),
            _article("Bitcoin price rally"),
        ]
        result = filter_by_keywords(arts, ["bitcoin"], min_score=0.0)
        # Only article with score > 0 should appear
        assert len(result) == 1
        assert "bitcoin" in result[0].title.lower()

    def test_updates_relevance_score(self):
        art = _article("Bitcoin news today")
        result = filter_by_keywords([art], ["bitcoin", "news"])
        assert result[0].relevance_score == 1.0

    def test_empty_articles(self):
        assert filter_by_keywords([], ["bitcoin"]) == []


# ── RelevanceScorer ───────────────────────────────────────────────────────────


class TestRelevanceScorer:
    def test_rank_returns_sorted(self):
        scorer = RelevanceScorer(use_semantic=False)
        arts = [
            _article("General news today"),
            _article("Bitcoin ETH crypto price news", "BTC rally"),
        ]
        ranked = scorer.rank("Will Bitcoin price exceed 100k?", arts, min_score=0.0)
        assert len(ranked) >= 1
        # Crypto article should rank higher
        assert "bitcoin" in ranked[0].title.lower() or "btc" in ranked[0].body.lower()

    def test_rank_empty_articles(self):
        scorer = RelevanceScorer(use_semantic=False)
        assert scorer.rank("Will Bitcoin hit 100k?", []) == []

    def test_rank_filters_below_threshold(self):
        scorer = RelevanceScorer(use_semantic=False)
        arts = [_article("Unrelated content about cats")]
        result = scorer.rank("Will Bitcoin hit 100k?", arts, min_score=0.5)
        assert result == []


# ── NewsStore ─────────────────────────────────────────────────────────────────


class TestNewsStore:
    def test_save_and_count(self, store: NewsStore):
        arts = [_article("Article one"), _article("Article two")]
        inserted = store.save(arts)
        assert inserted == 2
        assert store.count() == 2

    def test_dedup_by_url(self, store: NewsStore):
        art = _article("Duplicate article", url="https://example.com/dup")
        store.save([art])
        store.save([art])  # same URL — should not insert again
        assert store.count() == 1

    def test_get_articles_basic(self, store: NewsStore):
        store.save([_article("Test article", hours_ago=2.0)])
        result = store.get_articles(lookback_hours=6)
        assert len(result) == 1
        assert result[0].title == "Test article"

    def test_anti_lookahead_before_filters(self, store: NewsStore):
        """Articles published AFTER `before` must not be returned."""
        future_art = _article("Future article", hours_ago=-1.0)  # 1 hour in the future
        past_art = _article("Past article", hours_ago=5.0)
        store.save([future_art, past_art])

        now = datetime.now(tz=timezone.utc)
        result = store.get_articles(before=now, lookback_hours=48)
        titles = [a.title for a in result]

        assert "Past article" in titles
        assert "Future article" not in titles

    def test_anti_lookahead_historical_timestamp(self, store: NewsStore):
        """Using a historical `before` timestamp should exclude recent articles."""
        recent = _article("Recent article", hours_ago=1.0)
        old = _article("Old article", hours_ago=10.0)
        store.save([recent, old])

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=5)
        result = store.get_articles(before=cutoff, lookback_hours=48)
        titles = [a.title for a in result]

        assert "Old article" in titles
        assert "Recent article" not in titles

    def test_keyword_filter(self, store: NewsStore):
        store.save([
            _article("Bitcoin hits 100k"),
            _article("Football scores today"),
        ])
        result = store.get_articles(keywords=["bitcoin"], lookback_hours=12)
        assert all("bitcoin" in a.title.lower() for a in result)

    def test_expire_old(self, store: NewsStore, tmp_path: Path):
        # Insert article with old fetched_at — override via direct DB insert
        import sqlite3
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=100)).isoformat()
        conn = sqlite3.connect(str(tmp_path / "test_news.db"))
        conn.execute(
            "INSERT OR IGNORE INTO articles (url, source, title, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://old.com/art", "test", "Old article", old_ts, old_ts),
        )
        conn.commit()
        conn.close()

        deleted = store.expire_old(ttl_hours=72)
        assert deleted == 1
        assert store.count() == 0

    def test_save_empty_list(self, store: NewsStore):
        assert store.save([]) == 0

    def test_lookback_window(self, store: NewsStore):
        store.save([
            _article("Recent",  hours_ago=1.0),
            _article("Old",     hours_ago=100.0),
        ])
        result = store.get_articles(lookback_hours=6)
        titles = [a.title for a in result]
        assert "Recent" in titles
        assert "Old" not in titles


# ── compute_sentiment ─────────────────────────────────────────────────────────


class TestComputeSentiment:
    def test_empty_articles(self):
        ts = datetime.now(tz=timezone.utc)
        feat = compute_sentiment([], condition_id="cond1", timestamp=ts)
        assert feat.condition_id == "cond1"
        assert feat.article_count_24h == 0
        assert feat.avg_sentiment_score == 0.0

    def test_with_articles(self):
        arts = [
            _article("Great news! Bitcoin surges to record highs", hours_ago=2.0),
            _article("Terrible crash wipes out billions", hours_ago=6.0),
        ]
        ts = datetime.now(tz=timezone.utc)
        feat = compute_sentiment(arts, condition_id="cond2", timestamp=ts)
        assert feat.article_count_24h is not None
        assert feat.avg_sentiment_score is not None
        assert isinstance(feat.avg_sentiment_score, float)

    def test_sentiment_fields_present(self):
        arts = [_article("Positive market rally", hours_ago=1.0)]
        feat = compute_sentiment(arts, condition_id="cond3")
        assert feat.article_count_24h is not None
        assert feat.article_count_delta is not None
        assert feat.sentiment_std is not None
        assert feat.price_vs_sentiment_gap is None  # filled by caller


class TestScoreText:
    def test_positive_text(self):
        score = score_text("Excellent results! Great success!")
        assert score > 0.0

    def test_negative_text(self):
        score = score_text("Terrible crash, disaster, catastrophe")
        assert score < 0.0

    def test_neutral_empty(self):
        assert score_text("") == 0.0
        assert score_text("   ") == 0.0

    def test_output_range(self):
        score = score_text("The market opened today.")
        assert -1.0 <= score <= 1.0


# ── LunarCrush normalizer ─────────────────────────────────────────────────────


class TestIsApplicable:
    def test_crypto_category(self):
        assert is_applicable("crypto", "Any question") is True

    def test_bitcoin_keyword(self):
        assert is_applicable(None, "Will Bitcoin exceed 100k?") is True

    def test_ethereum_keyword(self):
        assert is_applicable("news", "Will Ethereum 2.0 launch?") is True

    def test_not_applicable(self):
        assert is_applicable("sports", "Will Real Madrid win the league?") is False


class TestExtractTopic:
    def test_bitcoin(self):
        assert extract_topic("Will Bitcoin hit 100k?") == "bitcoin"

    def test_ethereum(self):
        assert extract_topic("Will Ethereum reach 5k?") == "ethereum"

    def test_default(self):
        assert extract_topic("Will this happen?") == "crypto"


class TestNormalizeTopicResponse:
    def test_valid_0_to_5_scale(self):
        data = {"sentiment": 3.75, "posts_active": 1200, "galaxy_score": 65.0}
        reading = normalize_topic_response("bitcoin", data)
        assert reading is not None
        assert reading.topic == "bitcoin"
        assert reading.source == "lunarcrush"
        assert -1.0 <= reading.sentiment <= 1.0
        assert reading.sentiment > 0  # 3.75 > 2.5 → positive
        assert reading.posts_active == 1200
        assert reading.galaxy_score == 65.0

    def test_valid_already_normalized(self):
        data = {"sentiment": 0.7}
        reading = normalize_topic_response("eth", data)
        assert reading is not None
        assert reading.sentiment == 0.7

    def test_empty_data_returns_none(self):
        assert normalize_topic_response("btc", {}) is None

    def test_missing_sentiment_returns_none(self):
        data = {"posts_active": 500}
        assert normalize_topic_response("btc", data) is None

    def test_clamps_to_range(self):
        data = {"sentiment": 100.0}  # extreme value
        reading = normalize_topic_response("btc", data)
        assert reading is not None
        assert reading.sentiment <= 1.0


class TestNormalizeTimeSeriesResponse:
    def test_basic_series(self):
        data = [
            {"time": 1_700_000_000, "sentiment": 3.5},
            {"time": 1_700_003_600, "sentiment": 4.0},
        ]
        readings = normalize_time_series_response("bitcoin", data)
        assert len(readings) == 2
        assert readings[0].timestamp < readings[1].timestamp

    def test_skips_missing_timestamp(self):
        data = [
            {"sentiment": 3.0},  # no time field
            {"time": 1_700_000_000, "sentiment": 2.5},
        ]
        readings = normalize_time_series_response("bitcoin", data)
        assert len(readings) == 1

    def test_empty_data(self):
        assert normalize_time_series_response("bitcoin", []) == []


class TestLunarCrushSource:
    def test_inject_and_get_cached(self):
        source = LunarCrushSource()
        reading = SentimentReading(
            topic="bitcoin",
            source="lunarcrush",
            sentiment=0.5,
            timestamp=datetime.now(tz=timezone.utc),
        )
        source.inject_sentiment("bitcoin", reading)
        cached = source.get_cached("bitcoin")
        assert cached is not None
        assert cached.sentiment == 0.5

    def test_get_cached_missing(self):
        source = LunarCrushSource()
        assert source.get_cached("nonexistent") is None

    def test_case_insensitive_key(self):
        source = LunarCrushSource()
        reading = SentimentReading(
            topic="BTC",
            source="lunarcrush",
            sentiment=0.3,
            timestamp=datetime.now(tz=timezone.utc),
        )
        source.inject_sentiment("BTC", reading)
        assert source.get_cached("btc") is not None
