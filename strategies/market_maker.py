"""
Passive market making / spread capture strategy.

Status: stub — to be implemented in Phase 3.
"""
from __future__ import annotations

from config.schemas import PortfolioState, TradeSignal
from strategies.base_strategy import BaseStrategy


class MarketMaker(BaseStrategy):
    name = "market_maker"

    def generate_signals(
        self,
        market_data: dict,
        portfolio: PortfolioState,
    ) -> list[TradeSignal]:
        raise NotImplementedError("MarketMaker will be implemented in Phase 3")
