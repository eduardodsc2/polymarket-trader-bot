"""
Compare LLM probability to market price → trade signal.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import LLMEstimate, TradeSignal


class DecisionEngine:
    def decide(self, estimate: LLMEstimate, market_price: float, condition_id: str) -> TradeSignal | None:
        raise NotImplementedError("DecisionEngine will be implemented in Phase 4")
