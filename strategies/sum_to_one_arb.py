"""
Sum-to-one arbitrage strategy.

Edge: When YES price + NO price < $1, buying both tokens guarantees a profit
at resolution. This is risk-free arbitrage (very rare on liquid markets).

Status: stub — to be implemented in Phase 3.
"""
from __future__ import annotations

from config.schemas import PortfolioState, TradeSignal
from strategies.base_strategy import BaseStrategy


class SumToOneArb(BaseStrategy):
    name = "sum_to_one_arb"

    def generate_signals(
        self,
        market_data: dict,
        portfolio: PortfolioState,
    ) -> list[TradeSignal]:
        raise NotImplementedError("SumToOneArb will be implemented in Phase 3")
