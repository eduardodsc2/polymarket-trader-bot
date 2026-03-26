"""
Format prompt with news context + market data.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class PromptBuilder:
    def build(self, question: str, market_price: float, news_context: str, category: str = "base") -> str:
        raise NotImplementedError("PromptBuilder will be implemented in Phase 4")
