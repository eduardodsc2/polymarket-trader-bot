"""Unit tests for data/validate.py.

All functions under test are pure: no network, DB, or filesystem access.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.schemas import PricePoint
from data.validate import (
    AnomalyDetail,
    build_quality_report,
    check_duplicates,
    check_negative_volume,
    check_price_range,
    check_timestamp_gaps,
    check_yes_no_sum,
)


def _pt(token_id: str, price: float, hour: int = 0, volume: float | None = None) -> PricePoint:
    """Helper to build a PricePoint at a fixed UTC timestamp."""
    ts = datetime(2024, 1, 1, hour, 0, 0, tzinfo=timezone.utc)
    return PricePoint(token_id=token_id, timestamp=ts, price=price, volume=volume)


# ── check_price_range ──────────────────────────────────────────────────────────

class TestCheckPriceRange:
    def test_valid_prices_pass(self) -> None:
        prices = [_pt("tok", 0.0), _pt("tok", 0.5), _pt("tok", 1.0)]
        assert check_price_range(prices) == []

    def test_price_above_one_flagged(self) -> None:
        # PricePoint enforces [0,1] via Pydantic — check_price_range is a
        # secondary safeguard for data loaded directly from DB (bypassing validation).
        # Build a mock that quacks like PricePoint but has an invalid price.
        from unittest.mock import MagicMock
        bad = MagicMock(spec=["token_id", "timestamp", "price"])
        bad.token_id = "tok"
        bad.timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bad.price = 1.5
        anomalies = check_price_range([bad])
        assert len(anomalies) == 1
        assert "1.5" in anomalies[0].issue

    def test_empty_list(self) -> None:
        assert check_price_range([]) == []


# ── check_timestamp_gaps ───────────────────────────────────────────────────────

class TestCheckTimestampGaps:
    def test_no_gaps(self) -> None:
        prices = [_pt("tok", 0.5, hour=h) for h in range(6)]
        anomalies = check_timestamp_gaps(prices, max_gap=timedelta(hours=2))
        assert anomalies == []

    def test_gap_detected(self) -> None:
        t0 = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 1, 1, 5, tzinfo=timezone.utc)  # 5h gap
        prices = [
            PricePoint(token_id="tok", timestamp=t0, price=0.5),
            PricePoint(token_id="tok", timestamp=t1, price=0.6),
        ]
        anomalies = check_timestamp_gaps(prices, max_gap=timedelta(hours=2))
        assert len(anomalies) == 1
        assert "5:00:00" in anomalies[0].issue

    def test_multiple_tokens_independent(self) -> None:
        t0 = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 1, 1, 5, tzinfo=timezone.utc)
        prices = [
            PricePoint(token_id="tok_a", timestamp=t0, price=0.5),
            PricePoint(token_id="tok_a", timestamp=t1, price=0.6),
            PricePoint(token_id="tok_b", timestamp=t0, price=0.3),
            PricePoint(token_id="tok_b", timestamp=t1, price=0.4),
        ]
        anomalies = check_timestamp_gaps(prices, max_gap=timedelta(hours=2))
        assert len(anomalies) == 2
        assert all(a.token_id in ("tok_a", "tok_b") for a in anomalies)

    def test_empty_list(self) -> None:
        assert check_timestamp_gaps([]) == []


# ── check_duplicates ───────────────────────────────────────────────────────────

class TestCheckDuplicates:
    def test_no_duplicates(self) -> None:
        prices = [_pt("tok", 0.5, hour=h) for h in range(3)]
        assert check_duplicates(prices) == []

    def test_duplicate_detected(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        prices = [
            PricePoint(token_id="tok", timestamp=ts, price=0.5),
            PricePoint(token_id="tok", timestamp=ts, price=0.6),
        ]
        anomalies = check_duplicates(prices)
        assert len(anomalies) == 1
        assert "duplicate" in anomalies[0].issue

    def test_different_tokens_same_timestamp_ok(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        prices = [
            PricePoint(token_id="tok_a", timestamp=ts, price=0.5),
            PricePoint(token_id="tok_b", timestamp=ts, price=0.5),
        ]
        assert check_duplicates(prices) == []


# ── check_negative_volume ──────────────────────────────────────────────────────

class TestCheckNegativeVolume:
    def test_positive_volume_ok(self) -> None:
        prices = [_pt("tok", 0.5, volume=100.0)]
        assert check_negative_volume(prices) == []

    def test_none_volume_ok(self) -> None:
        prices = [_pt("tok", 0.5, volume=None)]
        assert check_negative_volume(prices) == []

    def test_negative_volume_flagged(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Build manually to bypass Pydantic validator if any
        p = PricePoint(token_id="tok", timestamp=ts, price=0.5)
        object.__setattr__(p, "volume", -10.0)
        anomalies = check_negative_volume([p])
        assert len(anomalies) == 1
        assert "-10.0" in anomalies[0].issue


# ── check_yes_no_sum ───────────────────────────────────────────────────────────

class TestCheckYesNoSum:
    def test_valid_sum(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yes = [PricePoint(token_id="yes", timestamp=ts, price=0.65)]
        no = [PricePoint(token_id="no", timestamp=ts, price=0.35)]
        assert check_yes_no_sum(yes, no) == []

    def test_sum_deviation_flagged(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yes = [PricePoint(token_id="yes", timestamp=ts, price=0.65)]
        no = [PricePoint(token_id="no", timestamp=ts, price=0.50)]  # sum = 1.15 > 1.05
        anomalies = check_yes_no_sum(yes, no, tolerance=0.05)
        assert len(anomalies) == 1
        assert "1.15" in anomalies[0].issue

    def test_no_matching_timestamps(self) -> None:
        t0 = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
        yes = [PricePoint(token_id="yes", timestamp=t0, price=0.65)]
        no = [PricePoint(token_id="no", timestamp=t1, price=0.35)]
        assert check_yes_no_sum(yes, no) == []


# ── build_quality_report ───────────────────────────────────────────────────────

class TestBuildQualityReport:
    def test_clean_data(self) -> None:
        prices = [_pt("tok", 0.5, hour=h) for h in range(5)]
        report = build_quality_report(prices)

        assert report.total_price_points == 5
        assert report.anomaly_count == 0
        assert report.anomaly_rate_pct == 0.0
        assert report.is_clean is True
        assert "timestamp_gaps" in report.checks_passed
        assert "duplicates" in report.checks_passed

    def test_detects_gap(self) -> None:
        t0 = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 1, 1, 10, tzinfo=timezone.utc)
        prices = [
            PricePoint(token_id="tok", timestamp=t0, price=0.5),
            PricePoint(token_id="tok", timestamp=t1, price=0.5),
        ]
        report = build_quality_report(prices)
        assert "timestamp_gaps" in report.checks_failed

    def test_empty_input(self) -> None:
        report = build_quality_report([])
        assert report.total_price_points == 0
        assert report.anomaly_rate_pct == 0.0
        assert report.is_clean is True
