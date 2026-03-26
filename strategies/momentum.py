"""
Trend-following / momentum strategy.

Status: stub — to be implemented in Phase 3.
"""
from __future__ import annotations

from config.schemas import PortfolioState, TradeSignal
from strategies.base_strategy import BaseStrategy


class Momentum(BaseStrategy):
    name = "momentum"

    def generate_signals(
        self,
        market_data: dict,
        portfolio: PortfolioState,
    ) -> list[TradeSignal]:
        raise NotImplementedError("Momentum will be implemented in Phase 3")
