"""Abstract base class that all strategies must inherit."""
from __future__ import annotations

from abc import ABC, abstractmethod

from config.schemas import PortfolioState, TradeSignal


class BaseStrategy(ABC):
    """
    Every strategy must implement generate_signals().
    The backtest engine and live executor call this method at each tick.
    """

    name: str = "base"

    @abstractmethod
    def generate_signals(
        self,
        market_data: dict,
        portfolio: PortfolioState,
    ) -> list[TradeSignal]:
        """
        Given current market data and portfolio state, return a list of
        trade signals (may be empty if no opportunity is found).
        """
        ...

    def __repr__(self) -> str:
        return f"<Strategy: {self.name}>"
