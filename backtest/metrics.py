"""
Quantitative performance metrics.

Formulas:
  Brier Score    = mean((p_hat - outcome)^2)
  Sharpe Ratio   = (mean_return - rf) / std_return
  Sortino Ratio  = (mean_return - rf) / downside_std
  Max Drawdown   = max(peak - trough) / peak
  Win Rate       = wins / total_trades
  Expected Value = sum(p_i * payoff_i)
  Kelly Fraction = (p*b - q) / b
  CAGR           = (final / initial)^(1/years) - 1

Status: stub — to be implemented in Phase 2.
"""
from __future__ import annotations


def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    raise NotImplementedError


def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    raise NotImplementedError


def sortino_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    raise NotImplementedError


def max_drawdown(equity_curve: list[float]) -> float:
    raise NotImplementedError


def cagr(initial: float, final: float, years: float) -> float:
    raise NotImplementedError


def kelly_fraction(win_prob: float, win_payoff: float) -> float:
    """Full Kelly fraction. Use 25% of result in practice."""
    q = 1 - win_prob
    b = win_payoff
    return (win_prob * b - q) / b
