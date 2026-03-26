"""
Pre-trade and position-level risk checks.

Enforces the non-negotiable risk rules from CLAUDE.md:
  1. Max single position: 5% of total capital
  2. Kelly cap: Never bet more than 25% of Kelly optimal
  3. Min market liquidity: >$10k volume
  4. Min edge: >3% (after fees)
  5. Max open positions: 20
  6. Daily loss limit: auto-halt if daily PnL < -5% of capital
  7. Correlation limit: ≤3 correlated positions

Status: stub — to be implemented in Phase 5.
"""
from __future__ import annotations

from config.schemas import PortfolioState, TradeSignal


class RiskManager:
    def check(self, signal: TradeSignal, portfolio: PortfolioState) -> bool:
        """Return True if the signal passes all risk checks."""
        raise NotImplementedError("RiskManager will be implemented in Phase 5")
