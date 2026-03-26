"""
Query LLM with news context, parse output.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import LLMEstimate


class LLMEstimator:
    def estimate(self, condition_id: str, question: str, news_context: str) -> LLMEstimate:
        raise NotImplementedError("LLMEstimator will be implemented in Phase 4")
