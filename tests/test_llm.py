"""
Unit tests for Phase 4 LLM layer.

Coverage:
  - llm.response_parser  — prompt_hash, parse_response (valid, missing fields,
                           bad confidence, SOURCES=NONE)
  - llm.cache            — LLMCache: hit, miss (TTL expired), daily cost
  - llm.decision_engine  — compute_edge, should_trade, compute_kelly_size,
                           DecisionEngine.decide (signal / no signal)
  - llm.calibration      — brier_score, compare_brier_scores, fit_platt,
                           apply_platt, ProbabilityCalibrator fit/transform

No network calls. LLMCache uses a tmp_path SQLite file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.schemas import LLMEstimate
from llm.calibration import (
    ProbabilityCalibrator,
    apply_platt,
    brier_score,
    compare_brier_scores,
    fit_platt,
)
from llm.cache import LLMCache
from llm.decision_engine import (
    DecisionEngine,
    compute_edge,
    compute_kelly_size,
    should_trade,
)
from llm.response_parser import parse_response, prompt_hash


# ── Helpers ───────────────────────────────────────────────────────────────────


def _estimate(
    probability: float = 0.70,
    confidence: float = 1.0,
    condition_id: str = "cond1",
) -> LLMEstimate:
    return LLMEstimate(
        condition_id=condition_id,
        model="test",
        prompt_hash="abc123",
        probability=probability,
        confidence=confidence,
        reasoning="Test reasoning",
        sources=["reuters"],
    )


@pytest.fixture
def cache(tmp_path: Path) -> LLMCache:
    return LLMCache(db_path=tmp_path / "test_llm_cache.db")


# ── prompt_hash ───────────────────────────────────────────────────────────────


class TestPromptHash:
    def test_deterministic(self):
        text = "Will Bitcoin exceed $100k by end of 2025?"
        assert prompt_hash(text) == prompt_hash(text)

    def test_length(self):
        h = prompt_hash("anything")
        assert len(h) == 16

    def test_different_inputs_differ(self):
        assert prompt_hash("question A") != prompt_hash("question B")

    def test_hex_characters(self):
        h = prompt_hash("test")
        assert all(c in "0123456789abcdef" for c in h)


# ── parse_response ────────────────────────────────────────────────────────────


_VALID_RESPONSE = """\
PROBABILITY: 0.73
CONFIDENCE: HIGH
EDGE: +0.08
REASONING: The market fundamentals are strong based on recent data.
SOURCES: reuters, coindesk
"""


class TestParseResponse:
    def test_valid_response(self):
        est = parse_response(_VALID_RESPONSE, "cond1", "claude-sonnet", "prompt")
        assert est.probability == 0.73
        assert est.confidence == 1.0  # HIGH → 1.0
        assert est.reasoning is not None
        assert "reuters" in (est.sources or [])
        assert "coindesk" in (est.sources or [])

    def test_medium_confidence(self):
        raw = "PROBABILITY: 0.55\nCONFIDENCE: MEDIUM\nREASONING: ok\n"
        est = parse_response(raw, "cond1", "model", "")
        assert est.confidence == pytest.approx(0.66)

    def test_low_confidence(self):
        raw = "PROBABILITY: 0.40\nCONFIDENCE: LOW\nREASONING: uncertain\n"
        est = parse_response(raw, "cond1", "model", "")
        assert est.confidence == pytest.approx(0.33)

    def test_sources_none(self):
        raw = "PROBABILITY: 0.60\nCONFIDENCE: HIGH\nREASONING: test\nSOURCES: NONE\n"
        est = parse_response(raw, "cond1", "model", "")
        assert est.sources is None

    def test_missing_probability_raises(self):
        raw = "CONFIDENCE: HIGH\nREASONING: missing prob\n"
        with pytest.raises(ValueError, match="PROBABILITY"):
            parse_response(raw, "cond1", "model", "")

    def test_missing_confidence_raises(self):
        raw = "PROBABILITY: 0.50\nREASONING: missing conf\n"
        with pytest.raises(ValueError, match="CONFIDENCE"):
            parse_response(raw, "cond1", "model", "")

    def test_unknown_confidence_label_raises(self):
        raw = "PROBABILITY: 0.50\nCONFIDENCE: VERY_HIGH\nREASONING: x\n"
        with pytest.raises(ValueError, match="Unknown CONFIDENCE"):
            parse_response(raw, "cond1", "model", "")

    def test_probability_out_of_range_raises(self):
        raw = "PROBABILITY: 1.5\nCONFIDENCE: HIGH\nREASONING: x\n"
        with pytest.raises(ValueError):
            parse_response(raw, "cond1", "model", "")

    def test_stores_condition_id(self):
        est = parse_response(_VALID_RESPONSE, "my-condition", "model", "")
        assert est.condition_id == "my-condition"

    def test_prompt_hash_stored(self):
        prompt = "some prompt text"
        est = parse_response(_VALID_RESPONSE, "cond1", "model", prompt)
        assert est.prompt_hash == prompt_hash(prompt)

    def test_case_insensitive_labels(self):
        raw = "probability: 0.65\nconfidence: high\nreasoning: test\n"
        est = parse_response(raw, "cond1", "model", "")
        assert est.probability == 0.65


# ── LLMCache ──────────────────────────────────────────────────────────────────


class TestLLMCache:
    def test_cache_miss_returns_none(self, cache: LLMCache):
        result = cache.get("cond1", "nonexistent_hash")
        assert result is None

    def test_save_and_hit(self, cache: LLMCache):
        est = _estimate()
        cache.save(est, estimated_cost_usd=0.001)
        result = cache.get("cond1", "abc123")
        assert result is not None
        assert result.probability == 0.70
        assert result.confidence == 1.0

    def test_ttl_expiry(self, tmp_path: Path):
        """Entries older than TTL must not be returned."""
        import sqlite3

        db_path = tmp_path / "ttl_test.db"
        c = LLMCache(db_path=db_path)
        est = _estimate()

        # Save with an old created_at timestamp (100 hours ago)
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=100)).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(condition_id, prompt_hash, model, probability, confidence, "
            " reasoning, sources, estimated_cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cond1", "abc123", "test", 0.70, 1.0,
                "reasoning", None, 0.001, old_ts,
            ),
        )
        conn.commit()
        conn.close()

        # Default TTL is 6 hours — entry at 100h should be expired
        result = c.get("cond1", "abc123")
        assert result is None

    def test_daily_cost_accumulates(self, cache: LLMCache):
        e1 = _estimate(condition_id="c1")
        e2 = LLMEstimate(
            condition_id="c2", model="t", prompt_hash="xyz",
            probability=0.5, confidence=0.66,
        )
        cache.save(e1, estimated_cost_usd=0.003)
        cache.save(e2, estimated_cost_usd=0.007)
        assert cache.get_daily_cost() == pytest.approx(0.010, abs=1e-6)

    def test_upsert_replaces(self, cache: LLMCache):
        e1 = _estimate(probability=0.60)
        e2 = _estimate(probability=0.80)
        cache.save(e1)
        cache.save(e2)  # same (condition_id, prompt_hash)
        result = cache.get("cond1", "abc123")
        assert result is not None
        assert result.probability == 0.80


# ── compute_edge ──────────────────────────────────────────────────────────────


class TestComputeEdge:
    def test_positive_edge(self):
        edge = compute_edge(llm_probability=0.70, market_price=0.50)
        assert edge == pytest.approx(0.20)

    def test_negative_edge(self):
        edge = compute_edge(llm_probability=0.30, market_price=0.50)
        assert edge == pytest.approx(-0.20)

    def test_confidence_scales_edge(self):
        edge = compute_edge(llm_probability=0.70, market_price=0.50, confidence=0.5)
        assert edge == pytest.approx(0.10)

    def test_zero_edge(self):
        assert compute_edge(0.50, 0.50) == 0.0


# ── should_trade ──────────────────────────────────────────────────────────────


class TestShouldTrade:
    def test_sufficient_edge(self):
        assert should_trade(edge=0.10, min_edge=0.05) is True

    def test_insufficient_edge(self):
        assert should_trade(edge=0.02, min_edge=0.05) is False

    def test_negative_edge_trades(self):
        assert should_trade(edge=-0.10, min_edge=0.05) is True

    def test_low_confidence_blocks_trade(self):
        # confidence=0.20 < threshold=0.33
        assert should_trade(edge=0.20, confidence=0.20, confidence_threshold=0.33) is False

    def test_medium_confidence_allows_trade(self):
        assert should_trade(edge=0.10, confidence=0.66, confidence_threshold=0.33) is True


# ── compute_kelly_size ────────────────────────────────────────────────────────


class TestComputeKellySize:
    def test_positive_edge_returns_positive_size(self):
        size = compute_kelly_size(edge=0.10, market_price=0.50, capital_usd=1000.0)
        assert size > 0

    def test_negative_edge_returns_positive_size(self):
        size = compute_kelly_size(edge=-0.10, market_price=0.50, capital_usd=1000.0)
        assert size > 0

    def test_zero_capital(self):
        size = compute_kelly_size(edge=0.10, market_price=0.50, capital_usd=0.0)
        assert size == 0.0

    def test_capped_by_max_position(self):
        # Very large capital with modest kelly fraction should still be capped
        size = compute_kelly_size(
            edge=0.40, market_price=0.50, capital_usd=1_000_000.0,
            kelly_fraction=0.25,
        )
        # Max position is 5% of capital by default settings
        assert size <= 1_000_000.0 * 0.05 + 0.01  # small tolerance

    def test_negative_kelly_returns_zero(self):
        # Edge so small Kelly formula gives <= 0
        size = compute_kelly_size(edge=0.001, market_price=0.999, capital_usd=1000.0)
        assert size == 0.0


# ── DecisionEngine ────────────────────────────────────────────────────────────


class TestDecisionEngine:
    def test_returns_signal_when_sufficient_edge(self):
        engine = DecisionEngine()
        est = _estimate(probability=0.75, confidence=1.0)
        signal = engine.decide(est, market_price=0.50, condition_id="c1",
                               token_id="tok1", capital_usd=1000.0)
        assert signal is not None
        assert signal.side == "BUY"
        assert signal.edge > 0

    def test_returns_none_when_edge_insufficient(self):
        engine = DecisionEngine()
        # LLM says 0.52, market says 0.50 → edge=0.02 < min_edge_pct=0.03
        est = _estimate(probability=0.52, confidence=1.0)
        signal = engine.decide(est, market_price=0.50, condition_id="c1",
                               token_id="tok1", capital_usd=1000.0)
        assert signal is None

    def test_returns_none_on_low_confidence(self):
        engine = DecisionEngine()
        # confidence=0.20 → below threshold 0.33
        est = _estimate(probability=0.80, confidence=0.20)
        signal = engine.decide(est, market_price=0.50, condition_id="c1",
                               token_id="tok1", capital_usd=1000.0)
        assert signal is None

    def test_sell_signal_when_llm_below_market(self):
        engine = DecisionEngine()
        est = _estimate(probability=0.25, confidence=1.0)
        signal = engine.decide(est, market_price=0.50, condition_id="c1",
                               token_id="tok1", capital_usd=1000.0)
        assert signal is not None
        assert signal.side == "SELL"
        assert signal.edge < 0

    def test_no_trade_zero_capital(self):
        engine = DecisionEngine()
        est = _estimate(probability=0.75, confidence=1.0)
        # capital_usd=0 → default size 10.0 still fires
        signal = engine.decide(est, market_price=0.50, condition_id="c1",
                               token_id="tok1", capital_usd=0.0)
        # With 0 capital the engine uses default 10.0 — signal should still be produced
        assert signal is not None


# ── brier_score ───────────────────────────────────────────────────────────────


class TestBrierScore:
    def test_perfect_predictor(self):
        probs = [1.0, 0.0, 1.0, 0.0]
        outcomes = [1, 0, 1, 0]
        assert brier_score(probs, outcomes) == 0.0

    def test_uninformative_predictor(self):
        probs = [0.5] * 4
        outcomes = [1, 0, 1, 0]
        assert brier_score(probs, outcomes) == pytest.approx(0.25)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            brier_score([0.5, 0.5], [1])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            brier_score([], [])

    def test_known_value(self):
        probs = [0.8, 0.2]
        outcomes = [1, 0]
        # (0.8-1)^2 + (0.2-0)^2 = 0.04 + 0.04 = 0.08 / 2 = 0.04
        assert brier_score(probs, outcomes) == pytest.approx(0.04)


# ── compare_brier_scores ──────────────────────────────────────────────────────


class TestCompareBrierScores:
    def test_improvement_when_news_is_better(self):
        outcomes = [1, 0, 1, 0]
        llm_only = [0.5, 0.5, 0.5, 0.5]       # uninformative
        llm_news  = [0.9, 0.1, 0.9, 0.1]       # well calibrated
        comp = compare_brier_scores(llm_only, llm_news, outcomes)
        assert comp.improvement > 0
        assert comp.improvement_pct > 0
        assert comp.llm_only > comp.llm_plus_news

    def test_named_tuple_fields(self):
        comp = compare_brier_scores([0.5], [0.5], [1])
        assert hasattr(comp, "llm_only")
        assert hasattr(comp, "llm_plus_news")
        assert hasattr(comp, "improvement")
        assert hasattr(comp, "improvement_pct")


# ── ProbabilityCalibrator ─────────────────────────────────────────────────────


class TestProbabilityCalibrator:
    def _training_data(self):
        # 20 synthetic (probability, outcome) pairs
        probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95,
                 0.1, 0.3, 0.5, 0.7, 0.9, 0.2, 0.4, 0.6, 0.8, 0.55]
        outcomes = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1,
                    0, 0, 0, 1, 1, 0, 1, 1, 1, 0]
        return probs, outcomes

    def test_fit_sets_fitted_flag(self):
        cal = ProbabilityCalibrator()
        assert not cal._fitted
        probs, outcomes = self._training_data()
        cal.fit(probs, outcomes)
        assert cal._fitted

    def test_transform_before_fit_returns_raw(self):
        cal = ProbabilityCalibrator()
        result = cal.transform(0.70)
        assert result == 0.70  # passthrough

    def test_transform_after_fit_returns_float(self):
        cal = ProbabilityCalibrator()
        probs, outcomes = self._training_data()
        cal.fit(probs, outcomes)
        result = cal.transform(0.70)
        assert isinstance(result, float)
        assert 0.001 <= result <= 0.999

    def test_calibrate_alias(self):
        cal = ProbabilityCalibrator()
        probs, outcomes = self._training_data()
        cal.fit(probs, outcomes)
        assert cal.calibrate(0.60) == cal.transform(0.60)

    def test_fit_empty_raises(self):
        cal = ProbabilityCalibrator()
        with pytest.raises(ValueError):
            cal.fit([], [])

    def test_fit_length_mismatch_raises(self):
        cal = ProbabilityCalibrator()
        with pytest.raises(ValueError):
            cal.fit([0.5, 0.6], [1])

    def test_save_and_load(self, tmp_path: Path):
        cal = ProbabilityCalibrator()
        probs, outcomes = self._training_data()
        cal.fit(probs, outcomes)

        path = tmp_path / "cal.json"
        cal.save(path)

        cal2 = ProbabilityCalibrator()
        cal2.load(path)

        assert cal2._fitted
        assert cal2._n_samples == len(probs)
        assert cal2.transform(0.70) == pytest.approx(cal.transform(0.70), abs=1e-4)

    def test_load_nonexistent_raises(self, tmp_path: Path):
        cal = ProbabilityCalibrator()
        with pytest.raises(FileNotFoundError):
            cal.load(tmp_path / "missing.json")


# ── fit_platt / apply_platt ───────────────────────────────────────────────────


class TestPlattFunctions:
    def test_fit_platt_returns_tuple(self):
        probs = [0.3, 0.5, 0.7, 0.8, 0.6, 0.4, 0.9, 0.2, 0.7, 0.5]
        outcomes = [0, 0, 1, 1, 1, 0, 1, 0, 1, 0]
        a, b = fit_platt(probs, outcomes)
        assert isinstance(a, float)
        assert isinstance(b, float)

    def test_apply_platt_in_range(self):
        result = apply_platt(0.70, a=1.0, b=0.0)
        assert 0.001 <= result <= 0.999

    def test_apply_platt_neutral(self):
        # a=1, b=0 → sigmoid(1*0.5 + 0) ≈ 0.622
        result = apply_platt(0.5, a=1.0, b=0.0)
        assert 0.5 < result < 0.75  # slightly above 0.5 due to sigmoid shape
