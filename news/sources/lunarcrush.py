"""
LunarCrush social sentiment adapter.

Fetches real-time and historical social sentiment from the LunarCrush MCP tool.
Used exclusively for crypto-category Polymarket markets.

NOTE: This module does NOT call LunarCrush directly — it is designed to be called
by the NewsFetcher orchestrator which invokes the LunarCrush MCP tools externally
and passes the result data in. In automated/backtest contexts the orchestrator
passes pre-fetched data via `inject_sentiment()`.

For live usage the caller (e.g. scripts or live executor) must call the MCP tools
and pass results here for normalization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from config.schemas import SentimentReading

# Market categories that have LunarCrush-trackable assets
_CRYPTO_CATEGORIES = frozenset({"crypto", "bitcoin", "ethereum", "defi", "nft", "web3"})

# Keywords in market questions that suggest a crypto asset
_CRYPTO_KEYWORDS = (
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
    "defi", "nft", "polygon", "matic", "solana", "sol", "binance",
    "coinbase", "polymarket", "usdc", "stablecoin",
)


def is_applicable(market_category: str | None, question: str) -> bool:
    """Return True if this market has a LunarCrush-trackable asset.

    Args:
        market_category: Market category string from Gamma API.
        question: Full market question text.
    """
    if market_category and market_category.lower() in _CRYPTO_CATEGORIES:
        return True
    q_lower = question.lower()
    return any(kw in q_lower for kw in _CRYPTO_KEYWORDS)


def extract_topic(question: str) -> str:
    """Extract the most relevant LunarCrush topic keyword from a market question.

    Returns the first matching crypto keyword found, defaulting to "crypto".
    """
    q_lower = question.lower()
    for kw in _CRYPTO_KEYWORDS:
        if kw in q_lower:
            return kw
    return "crypto"


def normalize_topic_response(
    topic: str,
    data: dict[str, Any],
    timestamp: datetime | None = None,
) -> SentimentReading | None:
    """Normalize a LunarCrush Topic MCP response into a SentimentReading.

    Args:
        topic: The topic string queried (e.g. "bitcoin").
        data: Raw dict returned by the LunarCrush Topic MCP tool.
        timestamp: Override timestamp (defaults to now UTC).

    Returns:
        SentimentReading if data is usable, None if essential fields are missing.
    """
    if not data:
        logger.warning("LunarCrush: empty data for topic", topic=topic)
        return None

    # LunarCrush Topic response fields
    raw_sentiment = data.get("sentiment") or data.get("sentiment_relative")
    if raw_sentiment is None:
        logger.warning("LunarCrush: no sentiment field in response", topic=topic, keys=list(data.keys()))
        return None

    # Normalize sentiment to [-1, +1] — LunarCrush returns 0–5 scale
    # 0=very bearish, 2.5=neutral, 5=very bullish → map to [-1, +1]
    raw_val = float(raw_sentiment)
    if raw_val > 1.0:
        # Looks like 0–5 scale
        normalized = (raw_val - 2.5) / 2.5
        normalized = max(-1.0, min(1.0, normalized))
    else:
        # Already in some normalized form, clamp to [-1, 1]
        normalized = max(-1.0, min(1.0, raw_val))

    ts = timestamp or datetime.now(tz=timezone.utc)

    return SentimentReading(
        topic=topic,
        source="lunarcrush",
        sentiment=normalized,
        posts_active=data.get("posts_active"),
        interactions=data.get("interactions_24h") or data.get("interactions"),
        galaxy_score=data.get("galaxy_score"),
        timestamp=ts,
    )


def normalize_time_series_response(
    topic: str,
    data: list[dict[str, Any]],
) -> list[SentimentReading]:
    """Normalize a LunarCrush Topic_Time_Series MCP response.

    Args:
        topic: The topic string queried.
        data: List of time-series data points from the MCP tool.

    Returns:
        List of SentimentReading objects sorted by timestamp ascending.
    """
    readings: list[SentimentReading] = []

    for point in data:
        ts_raw = point.get("time") or point.get("timestamp")
        if ts_raw is None:
            continue
        try:
            ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        except (ValueError, TypeError):
            continue

        reading = normalize_topic_response(topic, point, timestamp=ts)
        if reading is not None:
            readings.append(reading)

    readings.sort(key=lambda r: r.timestamp)
    logger.debug(
        "LunarCrush time series normalized",
        topic=topic,
        points=len(readings),
    )
    return readings


class LunarCrushSource:
    """
    Adapter that normalizes LunarCrush MCP responses into SentimentReading objects.

    In live mode the caller invokes the MCP tools and passes raw data here.
    In backtest mode pre-fetched data is injected via inject_sentiment().
    """

    def __init__(self) -> None:
        # In-memory cache: topic → SentimentReading (for single session)
        self._cache: dict[str, SentimentReading] = {}

    def inject_sentiment(self, topic: str, reading: SentimentReading) -> None:
        """Store a pre-fetched sentiment reading (used in backtest/test mode)."""
        self._cache[topic.lower()] = reading

    def get_cached(self, topic: str) -> SentimentReading | None:
        """Return cached sentiment for a topic if available."""
        return self._cache.get(topic.lower())

    def from_topic_response(
        self,
        topic: str,
        raw_data: dict[str, Any],
    ) -> SentimentReading | None:
        """Normalize and cache a Topic MCP response."""
        reading = normalize_topic_response(topic, raw_data)
        if reading:
            self._cache[topic.lower()] = reading
        return reading

    def from_time_series_response(
        self,
        topic: str,
        raw_data: list[dict[str, Any]],
    ) -> list[SentimentReading]:
        """Normalize a Topic_Time_Series MCP response."""
        return normalize_time_series_response(topic, raw_data)
