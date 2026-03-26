"""
Platt scaling calibration for LLM probability estimates.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class ProbabilityCalibrator:
    def fit(self, raw_probs: list[float], outcomes: list[int]) -> None:
        raise NotImplementedError("ProbabilityCalibrator will be implemented in Phase 4")

    def transform(self, raw_prob: float) -> float:
        raise NotImplementedError("ProbabilityCalibrator will be implemented in Phase 4")
