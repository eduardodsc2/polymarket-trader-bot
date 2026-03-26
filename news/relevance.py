"""
Article relevance scoring: match articles to a market question.

Uses keyword matching + semantic similarity (sentence-transformers).

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class RelevanceScorer:
    def score(self, question: str, article_title: str, article_body: str = "") -> float:
        raise NotImplementedError("RelevanceScorer will be implemented in Phase 4")
