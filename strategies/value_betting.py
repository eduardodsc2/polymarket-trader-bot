"""
LLM-assisted mispricing detection strategy.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations

from config.schemas import PortfolioState, TradeSignal
from strategies.base_strategy import BaseStrategy


class ValueBetting(BaseStrategy):
    name = "value_betting"

    def generate_signals(
        self,
        market_data: dict,
        portfolio: PortfolioState,
    ) -> list[TradeSignal]:
        raise NotImplementedError("ValueBetting will be implemented in Phase 4")
