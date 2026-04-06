"""
Unit tests for WeatherBetting pure functions.

No network calls, no DB, no filesystem. All functions are pure and
receive all dependencies via arguments.
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from data.fetchers.weather_fetcher import parse_daily_max, parse_weather_buckets
from strategies.weather_betting import (
    compute_bucket_probability,
    compute_edge,
    compute_kelly_size,
    is_weather_market,
    normalize_bucket_probabilities,
    select_ladder_legs,
)
from config.schemas import WeatherBucket


# ── is_weather_market ─────────────────────────────────────────────────────────

class TestIsWeatherMarket:
    def test_celsius_symbol(self):
        assert is_weather_market("Will NYC high be above 27°C on April 8?") is True

    def test_fahrenheit_symbol(self):
        assert is_weather_market("Will Miami reach 90°F on April 9?") is True

    def test_temperature_keyword(self):
        assert is_weather_market("Will the temperature in Chicago exceed 20°C?") is True

    def test_high_temp_keyword(self):
        assert is_weather_market("NYC high temp above 75°F?") is True

    def test_unrelated_market(self):
        assert is_weather_market("Will BTC reach $100k by end of 2025?") is False

    def test_unrelated_market_with_numbers(self):
        assert is_weather_market("Will the Fed cut rates by 25 basis points?") is False


# ── compute_bucket_probability ────────────────────────────────────────────────

class TestComputeBucketProbability:
    def test_entire_range_probability_near_one(self):
        # Probability of being between -inf and +inf should be ~1.0
        p = compute_bucket_probability(-1000, 1000, mu=25.0, sigma=1.5, n_samples=10_000, random_seed=42)
        assert p > 0.999

    def test_probability_near_mean(self):
        # P(24 <= X < 26) around mean=25 with sigma=1.5 should be substantial
        p = compute_bucket_probability(24.0, 26.0, mu=25.0, sigma=1.5, n_samples=50_000, random_seed=42)
        assert 0.35 < p < 0.65

    def test_probability_far_from_mean_is_low(self):
        # P(35 <= X < inf) when mean=25, sigma=1.5 — very unlikely
        p = compute_bucket_probability(35.0, math.inf, mu=25.0, sigma=1.5, n_samples=50_000, random_seed=42)
        assert p < 0.001

    def test_seed_reproducibility(self):
        p1 = compute_bucket_probability(24.0, 26.0, mu=25.0, sigma=1.5, n_samples=10_000, random_seed=99)
        p2 = compute_bucket_probability(24.0, 26.0, mu=25.0, sigma=1.5, n_samples=10_000, random_seed=99)
        assert p1 == p2

    def test_different_seeds_give_different_results(self):
        p1 = compute_bucket_probability(24.0, 26.0, mu=25.0, sigma=1.5, n_samples=10_000, random_seed=1)
        p2 = compute_bucket_probability(24.0, 26.0, mu=25.0, sigma=1.5, n_samples=10_000, random_seed=2)
        assert p1 != p2

    def test_probability_in_valid_range(self):
        p = compute_bucket_probability(20.0, 30.0, mu=25.0, sigma=2.0, n_samples=10_000, random_seed=42)
        assert 0.0 <= p <= 1.0


# ── normalize_bucket_probabilities ────────────────────────────────────────────

class TestNormalizeBucketProbabilities:
    def test_already_normalized(self):
        result = normalize_bucket_probabilities([0.3, 0.4, 0.3])
        assert abs(sum(result) - 1.0) < 1e-10

    def test_unnormalized_input(self):
        result = normalize_bucket_probabilities([1.0, 2.0, 1.0])
        assert abs(result[0] - 0.25) < 1e-10
        assert abs(result[1] - 0.50) < 1e-10
        assert abs(result[2] - 0.25) < 1e-10

    def test_zero_sum_raises(self):
        with pytest.raises(ValueError):
            normalize_bucket_probabilities([0.0, 0.0, 0.0])

    def test_negative_sum_raises(self):
        with pytest.raises(ValueError):
            normalize_bucket_probabilities([-1.0, -2.0])

    def test_single_bucket(self):
        result = normalize_bucket_probabilities([0.7])
        assert abs(result[0] - 1.0) < 1e-10


# ── compute_edge ──────────────────────────────────────────────────────────────

class TestComputeEdge:
    def test_positive_edge(self):
        assert compute_edge(0.70, 0.50) == pytest.approx(0.20)

    def test_negative_edge(self):
        assert compute_edge(0.30, 0.50) == pytest.approx(-0.20)

    def test_zero_edge(self):
        assert compute_edge(0.50, 0.50) == pytest.approx(0.0)


# ── select_ladder_legs ────────────────────────────────────────────────────────

def _make_bucket(label, model_prob, market_price, token_id="tok1") -> WeatherBucket:
    return WeatherBucket(
        label=label,
        lo=0.0,
        hi=math.inf,
        model_prob=model_prob,
        market_price=market_price,
        edge=compute_edge(model_prob, market_price),
        token_id=token_id,
    )


class TestSelectLadderLegs:
    def test_selects_buckets_above_min_edge(self):
        buckets = [
            _make_bucket("A", model_prob=0.60, market_price=0.50),  # edge=0.10 = 10%
            _make_bucket("B", model_prob=0.52, market_price=0.50),  # edge=0.02 = 2%
        ]
        legs = select_ladder_legs(buckets, min_edge_pct=5.0, max_legs=4, min_price=0.0, max_price=1.0)
        assert len(legs) == 1
        assert legs[0].label == "A"

    def test_respects_max_legs(self):
        buckets = [_make_bucket(str(i), 0.70, 0.50) for i in range(10)]
        legs = select_ladder_legs(buckets, min_edge_pct=5.0, max_legs=3, min_price=0.0, max_price=1.0)
        assert len(legs) == 3

    def test_filters_by_price_range(self):
        buckets = [
            _make_bucket("cheap", model_prob=0.70, market_price=0.005),  # below min
            _make_bucket("valid", model_prob=0.70, market_price=0.30),
            _make_bucket("expensive", model_prob=0.70, market_price=0.80),  # above max
        ]
        legs = select_ladder_legs(buckets, min_edge_pct=5.0, max_legs=4, min_price=0.01, max_price=0.60)
        assert len(legs) == 1
        assert legs[0].label == "valid"

    def test_sorted_by_edge_descending(self):
        buckets = [
            _make_bucket("low_edge", model_prob=0.56, market_price=0.50),   # edge=6%
            _make_bucket("high_edge", model_prob=0.70, market_price=0.50),  # edge=20%
        ]
        legs = select_ladder_legs(buckets, min_edge_pct=5.0, max_legs=4, min_price=0.0, max_price=1.0)
        assert legs[0].label == "high_edge"

    def test_empty_when_no_edge(self):
        buckets = [_make_bucket("A", model_prob=0.50, market_price=0.50)]
        legs = select_ladder_legs(buckets, min_edge_pct=5.0, max_legs=4, min_price=0.0, max_price=1.0)
        assert legs == []


# ── compute_kelly_size ────────────────────────────────────────────────────────

class TestComputeKellySize:
    def test_positive_edge_returns_positive_size(self):
        size = compute_kelly_size(
            edge=0.20, market_price=0.50,
            kelly_fraction=0.25, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size > 0.0

    def test_capped_at_max_position(self):
        # Large edge should be capped
        size = compute_kelly_size(
            edge=0.40, market_price=0.50,
            kelly_fraction=1.0, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size <= 25.0

    def test_zero_edge_returns_zero(self):
        size = compute_kelly_size(
            edge=0.0, market_price=0.50,
            kelly_fraction=0.25, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size == 0.0

    def test_negative_edge_returns_zero(self):
        size = compute_kelly_size(
            edge=-0.10, market_price=0.50,
            kelly_fraction=0.25, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size == 0.0

    def test_invalid_market_price_zero(self):
        size = compute_kelly_size(
            edge=0.10, market_price=0.0,
            kelly_fraction=0.25, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size == 0.0

    def test_invalid_market_price_one(self):
        size = compute_kelly_size(
            edge=0.10, market_price=1.0,
            kelly_fraction=0.25, capital_usd=500.0, max_position_usd=25.0,
        )
        assert size == 0.0


# ── parse_daily_max ───────────────────────────────────────────────────────────

class TestParseDailyMax:
    def _make_raw(self, temps_by_hour: dict[str, float]) -> dict:
        times = list(temps_by_hour.keys())
        temps = list(temps_by_hour.values())
        return {"hourly": {"time": times, "temperature_2m": temps}}

    def test_extracts_max_for_target_date(self):
        raw = self._make_raw({
            "2026-04-08T00:00": 20.0,
            "2026-04-08T12:00": 28.0,
            "2026-04-08T18:00": 25.0,
            "2026-04-09T00:00": 22.0,
        })
        result = parse_daily_max(raw, date(2026, 4, 8))
        assert result == 28.0

    def test_raises_for_missing_date(self):
        raw = self._make_raw({"2026-04-08T12:00": 25.0})
        with pytest.raises(ValueError):
            parse_daily_max(raw, date(2026, 4, 9))

    def test_ignores_none_values(self):
        raw = {"hourly": {
            "time": ["2026-04-08T00:00", "2026-04-08T12:00"],
            "temperature_2m": [None, 27.5],
        }}
        result = parse_daily_max(raw, date(2026, 4, 8))
        assert result == 27.5


# ── parse_weather_buckets ─────────────────────────────────────────────────────

class TestParseWeatherBuckets:
    def test_single_celsius_threshold(self):
        result = parse_weather_buckets("Will NYC high be above 27°C on April 8?")
        assert len(result) == 1
        lo, hi, label = result[0]
        assert lo == pytest.approx(27.0)
        assert math.isinf(hi)
        assert "27" in label

    def test_single_fahrenheit_threshold(self):
        result = parse_weather_buckets("Will Miami reach 90°F on April 9?")
        assert len(result) == 1
        lo, hi, label = result[0]
        assert lo == pytest.approx((90 - 32) * 5 / 9, rel=1e-3)
        assert math.isinf(hi)

    def test_range_buckets(self):
        result = parse_weather_buckets("NYC high: 70-75°F / 75-80°F / 80-85°F?")
        assert len(result) == 3
        # Sorted by lo ascending
        assert result[0][0] < result[1][0] < result[2][0]
        # Last bucket is open-ended
        assert math.isinf(result[-1][1])

    def test_no_temperature_returns_empty(self):
        result = parse_weather_buckets("Will BTC reach $100k?")
        assert result == []
