"""
Sentiment scoring using VADER (local, no API required).

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class SentimentScorer:
    def score(self, text: str) -> float:
        """Return sentiment score in [-1, +1]."""
        raise NotImplementedError("SentimentScorer will be implemented in Phase 4")
