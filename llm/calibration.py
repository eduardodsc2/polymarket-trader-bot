"""
Platt scaling calibration for LLM probability estimates.

Fits a sigmoid (logistic) transformation on (raw_probability, outcome) pairs
to correct systematic over/under-confidence in raw LLM estimates.

Calibration workflow:
    1. Collect (probability, outcome) pairs from resolved markets.
    2. Call ProbabilityCalibrator.fit() to train the sigmoid.
    3. Call ProbabilityCalibrator.calibrate() on future raw estimates.
    4. Use compare_brier_scores() to quantify the improvement.

All computation functions are pure — serialization/IO is isolated to save/load.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NamedTuple

from loguru import logger

_DEFAULT_MODEL_PATH = Path("data/calibration_model.json")


# ── Pure metrics ──────────────────────────────────────────────────────────────

def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    """Compute the Brier Score for a list of probability estimates.

    Brier Score = mean((p_hat - outcome)^2)
    Lower is better. Perfect = 0.0, Uninformative = 0.25.

    Args:
        probabilities: List of predicted probabilities in [0, 1].
        outcomes: List of binary outcomes (0 or 1).

    Returns:
        Brier score in [0, 1].

    Raises:
        ValueError: If lists are empty or have different lengths.
    """
    if not probabilities or not outcomes:
        raise ValueError("probabilities and outcomes must be non-empty")
    if len(probabilities) != len(outcomes):
        raise ValueError(
            f"Length mismatch: {len(probabilities)} probabilities vs {len(outcomes)} outcomes"
        )
    n = len(probabilities)
    total = sum((p - o) ** 2 for p, o in zip(probabilities, outcomes))
    return round(total / n, 6)


class BrierComparison(NamedTuple):
    llm_only: float
    llm_plus_news: float
    improvement: float       # positive = LLM+news is better (lower score)
    improvement_pct: float


def compare_brier_scores(
    llm_only_probs: list[float],
    llm_news_probs: list[float],
    outcomes: list[int],
) -> BrierComparison:
    """Compare Brier Scores for LLM-only vs LLM+news estimates.

    Returns a named tuple with both scores and the improvement delta.
    """
    score_only = brier_score(llm_only_probs, outcomes)
    score_news = brier_score(llm_news_probs, outcomes)
    improvement = score_only - score_news
    improvement_pct = (improvement / score_only * 100) if score_only > 0 else 0.0
    return BrierComparison(
        llm_only=score_only,
        llm_plus_news=score_news,
        improvement=round(improvement, 6),
        improvement_pct=round(improvement_pct, 2),
    )


# ── Platt scaling ─────────────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _log_loss(a: float, b: float, probs: list[float], outcomes: list[int]) -> float:
    """Log-loss for Platt scaling parameters (a, b)."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        fx = _sigmoid(a * p + b)
        fx = max(1e-12, min(1 - 1e-12, fx))
        total -= y * math.log(fx) + (1 - y) * math.log(1 - fx)
    return total / len(probs)


def fit_platt(
    probabilities: list[float],
    outcomes: list[int],
    lr: float = 0.01,
    n_iter: int = 1000,
) -> tuple[float, float]:
    """Fit Platt scaling parameters (a, b) via gradient descent.

    Platt scaling maps raw probability p → sigmoid(a*p + b).
    With enough data, this corrects systematic miscalibration.

    Args:
        probabilities: Raw LLM probability estimates in [0, 1].
        outcomes: Binary outcomes (0 or 1) for each estimate.
        lr: Learning rate for gradient descent.
        n_iter: Number of gradient descent iterations.

    Returns:
        Tuple (a, b) — the trained scaling parameters.
    """
    if len(probabilities) < 10:
        logger.warning(
            "Platt scaling: only %d samples — calibration may be unreliable (need >= 10)",
            len(probabilities),
        )

    a, b = 1.0, 0.0  # neutral init: sigmoid(1*p + 0) ≈ p for p near 0.5

    for _ in range(n_iter):
        grad_a = grad_b = 0.0
        for p, y in zip(probabilities, outcomes):
            fx = _sigmoid(a * p + b)
            err = fx - y
            grad_a += err * p
            grad_b += err
        n = len(probabilities)
        a -= lr * grad_a / n
        b -= lr * grad_b / n

    return a, b


def apply_platt(raw_prob: float, a: float, b: float) -> float:
    """Apply Platt scaling to a raw probability.

    Returns calibrated probability in (0, 1).
    """
    calibrated = _sigmoid(a * raw_prob + b)
    return round(max(0.001, min(0.999, calibrated)), 4)


# ── Serializable calibrator class ─────────────────────────────────────────────

class ProbabilityCalibrator:
    """Fits and applies Platt scaling to LLM probability estimates.

    Persist trained parameters with save() / load().
    """

    def __init__(self) -> None:
        self._a: float = 1.0
        self._b: float = 0.0
        self._fitted: bool = False
        self._n_samples: int = 0

    def fit(self, raw_probs: list[float], outcomes: list[int]) -> None:
        """Train Platt scaling on historical (probability, outcome) pairs."""
        if not raw_probs or not outcomes:
            raise ValueError("raw_probs and outcomes must be non-empty")
        if len(raw_probs) != len(outcomes):
            raise ValueError("raw_probs and outcomes must have the same length")

        self._a, self._b = fit_platt(raw_probs, outcomes)
        self._fitted = True
        self._n_samples = len(raw_probs)

        before = brier_score(raw_probs, outcomes)
        calibrated = [apply_platt(p, self._a, self._b) for p in raw_probs]
        after = brier_score(calibrated, outcomes)

        logger.info(
            "ProbabilityCalibrator: fit complete",
            n_samples=self._n_samples,
            a=round(self._a, 4),
            b=round(self._b, 4),
            brier_before=before,
            brier_after=after,
            improvement_pct=round((before - after) / before * 100, 1) if before > 0 else 0,
        )

    def transform(self, raw_prob: float) -> float:
        """Apply Platt scaling. Returns raw_prob unchanged if not fitted."""
        if not self._fitted:
            logger.warning("ProbabilityCalibrator: not fitted — returning raw probability")
            return raw_prob
        return apply_platt(raw_prob, self._a, self._b)

    # Alias for sklearn-style API
    calibrate = transform

    def save(self, path: Path | str = _DEFAULT_MODEL_PATH) -> None:
        """Persist calibration parameters to a JSON file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "a": self._a,
            "b": self._b,
            "fitted": self._fitted,
            "n_samples": self._n_samples,
        }
        p.write_text(json.dumps(data, indent=2))
        logger.info("ProbabilityCalibrator: saved to %s", p)

    def load(self, path: Path | str = _DEFAULT_MODEL_PATH) -> None:
        """Load calibration parameters from a JSON file."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Calibration model not found: {p}")
        data = json.loads(p.read_text())
        self._a = data["a"]
        self._b = data["b"]
        self._fitted = data.get("fitted", True)
        self._n_samples = data.get("n_samples", 0)
        logger.info(
            "ProbabilityCalibrator: loaded from %s (n=%d)", p, self._n_samples
        )
