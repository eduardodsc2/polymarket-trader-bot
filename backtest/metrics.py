"""
Quantitative performance metrics — all pure functions.

Every function is stateless: same inputs → same outputs. No I/O, no logging,
no DB calls. Tests can call them directly with known inputs.

Formulas:
  Sharpe Ratio   = (mean_return - rf) / std_return  * sqrt(252)  [annualized daily]
  Sortino Ratio  = (mean_return - rf) / downside_std * sqrt(252)
  Max Drawdown   = max((peak - trough) / peak)  over equity curve
  CAGR           = (final / initial)^(1/years) - 1
  Win Rate       = trades_with_positive_pnl / total_trades
  Brier Score    = mean((p_hat - outcome)^2)           [lower = better]
  Kelly Fraction = (p*b - q) / b   where b = win_payoff - 1
  Profit Factor  = gross_profit / |gross_loss|
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from config.schemas import BacktestMetrics, PortfolioSnapshot, Trade


# ── Individual metric functions ────────────────────────────────────────────────

def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """
    Annualized Sharpe ratio from daily returns.

    Returns 0.0 if std is zero (constant returns) or fewer than 2 data points.
    """
    arr = np.array(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free_rate
    std = float(np.std(excess, ddof=1))
    if std == 0.0:
        return 0.0
    return float(np.mean(excess) / std * np.sqrt(252))


def sortino_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """
    Annualized Sortino ratio from daily returns.

    Penalizes only downside volatility (returns below risk_free_rate).
    Returns 0.0 if no negative returns exist.
    """
    arr = np.array(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free_rate
    downside = excess[excess < 0]
    if len(downside) == 0:
        return 0.0
    downside_std = float(np.std(downside, ddof=1))
    if downside_std == 0.0:
        return 0.0
    return float(np.mean(excess) / downside_std * np.sqrt(252))


def max_drawdown(equity_curve: list[float]) -> float:
    """
    Maximum peak-to-trough drawdown as a fraction (0–1).

    Returns 0.0 for a curve with fewer than 2 points or no drawdown.
    """
    arr = np.array(equity_curve, dtype=float)
    if len(arr) < 2:
        return 0.0
    peaks = np.maximum.accumulate(arr)
    # Avoid division by zero for zero-value equity
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(peaks > 0, (peaks - arr) / peaks, 0.0)
    return float(np.max(drawdowns))


def cagr(initial: float, final: float, years: float) -> float:
    """
    Compound annual growth rate.

    Returns 0.0 if initial <= 0 or years <= 0.
    """
    if initial <= 0 or years <= 0 or final <= 0:
        return 0.0
    return float((final / initial) ** (1.0 / years) - 1.0)


def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    """
    Mean squared error between probability forecasts and binary outcomes.

    Args:
        probabilities: Forecast probability for outcome=1, in [0, 1].
        outcomes:      Actual binary outcome (0 or 1).

    Returns 0.0 for an empty list. Lower = better (0 = perfect).
    """
    if not probabilities:
        return 0.0
    p = np.array(probabilities, dtype=float)
    o = np.array(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


def kelly_fraction(win_prob: float, win_payoff: float) -> float:
    """
    Full Kelly fraction for a binary bet.

    Args:
        win_prob:    Probability of winning (0–1).
        win_payoff:  Net payoff per $1 risked if the bet wins
                     (e.g., 1.0 means doubling your money).

    Returns the fraction of capital to bet. Use 0.25× (fractional Kelly) in practice.
    """
    if win_payoff <= 0:
        return 0.0
    q = 1.0 - win_prob
    return (win_prob * win_payoff - q) / win_payoff


def profit_factor(trades: list[Trade]) -> float:
    """gross_profit / |gross_loss|. Returns 0.0 if no losses."""
    pnls = _trade_pnls(trades)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return 0.0
    return gross_profit / gross_loss


def win_rate(trades: list[Trade]) -> float:
    """Fraction of trades with positive PnL."""
    pnls = _trade_pnls(trades)
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)


# ── Aggregate metrics ──────────────────────────────────────────────────────────

def compute_metrics(
    strategy_name: str,
    snapshots: list[PortfolioSnapshot],
    trades: list[Trade],
    initial_capital: float,
    start_date: datetime,
    end_date: datetime,
    risk_free_rate: float = 0.0,
) -> BacktestMetrics:
    """
    Compute the full BacktestMetrics from a completed portfolio run.

    Args:
        strategy_name:  Name of the strategy (for the report).
        snapshots:      Ordered list of PortfolioSnapshot (equity curve points).
        trades:         All executed trades.
        initial_capital: Starting USDC balance.
        start_date:     Backtest period start.
        end_date:       Backtest period end.
        risk_free_rate: Daily risk-free rate for Sharpe/Sortino (default 0).

    Returns a fully populated BacktestMetrics Pydantic model.
    """
    final_capital = snapshots[-1].total_value_usd if snapshots else initial_capital

    equity_values = [s.total_value_usd for s in snapshots]
    daily_returns = _daily_returns_from_equity(equity_values)

    years = max(
        (end_date - start_date).total_seconds() / (365.25 * 86_400),
        1 / 365.25,  # minimum 1 day to avoid division by zero
    )

    total_return = (final_capital - initial_capital) / initial_capital if initial_capital else 0.0

    return BacktestMetrics(
        strategy=strategy_name,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        final_capital=final_capital,
        total_return_pct=total_return,
        cagr=cagr(initial_capital, final_capital, years),
        sharpe_ratio=sharpe_ratio(daily_returns, risk_free_rate),
        sortino_ratio=sortino_ratio(daily_returns, risk_free_rate),
        max_drawdown_pct=max_drawdown(equity_values),
        win_rate=win_rate(trades),
        total_trades=len(trades),
        brier_score=None,   # populated externally when probability estimates are available
        expected_value_per_trade=_expected_value(trades),
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _daily_returns_from_equity(equity: list[float]) -> list[float]:
    """Convert an equity curve to a list of daily percentage returns."""
    if len(equity) < 2:
        return []
    arr = np.array(equity, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.where(arr[:-1] > 0, (arr[1:] - arr[:-1]) / arr[:-1], 0.0)
    return returns.tolist()


def _trade_pnls(trades: list[Trade]) -> list[float]:
    """
    Per-trade PnL for closed positions only (SELL side).

    For MarketMaker (explicit SELL): net_proceeds = size_usd - fee_usd.
    For resolution-based strategies (CalibBetting, SumToOneArb): the portfolio
    emits a synthetic SELL Trade at fill_price=1.0 (win) or fill_price=0.0 (loss).
    PnL = size_usd - fee_usd: positive for wins (payout > 0), zero for losses.

    BUY trades are excluded — they have no realized PnL on their own.
    """
    pnls: list[float] = []
    for t in trades:
        if t.side == "SELL":
            pnls.append(t.size_usd - t.fee_usd)
    return pnls


def _expected_value(trades: list[Trade]) -> float | None:
    """Mean PnL per trade (BUY side excluded as explained in _trade_pnls)."""
    pnls = _trade_pnls(trades)
    if not pnls:
        return None
    return float(np.mean(pnls))
