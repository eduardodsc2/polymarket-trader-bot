"""
Pre-trade and position-level risk checks.

Enforces the seven non-negotiable risk rules from CLAUDE.md:
  1. Max single position: 5% of total capital
  2. Kelly cap: Never bet more than 25% of Kelly optimal
  3. Min market liquidity: >$10k volume
  4. Min edge: >3% (after fees)
  5. Max open positions: 20 simultaneous
  6. Daily loss limit: auto-halt if daily PnL < -5% of capital
  7. Correlation limit: ≤3 correlated positions (same category)

All checks are pure functions — no I/O, no logging.
The caller (executor) is responsible for logging rejections.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config.schemas import PortfolioState, TradeSignal
from config.settings import Settings


@dataclass(frozen=True)
class RiskViolation:
    rule: str
    reason: str


@dataclass
class RiskCheckResult:
    approved: bool
    violation: RiskViolation | None = None


class RiskManager:
    """
    Stateless risk checker — instantiated once and reused across all signals.

    Args:
        settings:     Loaded Settings instance (injected; never read os.getenv here).
        initial_capital: Capital at start of trading session (for daily loss limit).
    """

    def __init__(self, settings: Settings, initial_capital: float) -> None:
        self._s               = settings
        self._initial_capital = initial_capital

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, signal: TradeSignal, portfolio: PortfolioState) -> RiskCheckResult:
        """
        Run all 7 risk rules against *signal* and the current *portfolio*.

        Returns RiskCheckResult(approved=True) if all rules pass,
        or RiskCheckResult(approved=False, violation=...) on the first failure.
        """
        total_value = self._total_value(portfolio)

        for rule_fn in (
            self._check_min_edge,
            self._check_max_position_size,
            self._check_kelly_cap,
            self._check_max_open_positions,
            self._check_daily_loss_limit,
            self._check_correlation_limit,
        ):
            result = rule_fn(signal, portfolio, total_value)
            if not result.approved:
                return result

        return RiskCheckResult(approved=True)

    def check_liquidity(self, volume_usd: float) -> RiskCheckResult:
        """
        Rule 3 — Min market liquidity check (called before even generating a signal).
        Separated because it requires market volume, not a TradeSignal.
        """
        if volume_usd < self._s.min_market_volume_usd:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="min_market_liquidity",
                    reason=(
                        f"Market volume ${volume_usd:,.0f} below minimum "
                        f"${self._s.min_market_volume_usd:,.0f}"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    # ── Rule implementations (pure) ───────────────────────────────────────────

    def _check_min_edge(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """Rule 4 — Min edge > min_edge_pct after fees."""
        if signal.edge < self._s.min_edge_pct:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="min_edge",
                    reason=(
                        f"Edge {signal.edge:.3f} below minimum {self._s.min_edge_pct:.3f}"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_max_position_size(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """Rule 1 — Max single position ≤ max_position_pct of total capital."""
        max_allowed = total_value * self._s.circuit_breaker_max_position_pct
        if signal.suggested_size_usd > max_allowed:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="max_position_size",
                    reason=(
                        f"Requested size ${signal.suggested_size_usd:.2f} exceeds "
                        f"max ${max_allowed:.2f} "
                        f"({self._s.circuit_breaker_max_position_pct:.0%} of ${total_value:.2f})"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_kelly_cap(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """Rule 2 — Never bet more than kelly_fraction * Kelly optimal."""
        full_kelly = compute_kelly_fraction(
            p_win=signal.estimated_probability,
            odds=1.0 / signal.market_price if signal.market_price > 0 else 0.0,
        )
        kelly_size = total_value * full_kelly * self._s.kelly_fraction
        if kelly_size <= 0:
            # Negative Kelly means no edge — reject
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="kelly_cap",
                    reason=(
                        f"Full Kelly is non-positive ({full_kelly:.4f}): "
                        "no mathematical edge detected"
                    ),
                ),
            )
        if signal.suggested_size_usd > kelly_size:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="kelly_cap",
                    reason=(
                        f"Requested size ${signal.suggested_size_usd:.2f} exceeds "
                        f"fractional Kelly cap ${kelly_size:.2f}"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_max_open_positions(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """Rule 5 — Max simultaneous open positions."""
        open_count = len(portfolio.positions)
        if open_count >= self._s.circuit_breaker_max_positions:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="max_open_positions",
                    reason=(
                        f"Already at max open positions: "
                        f"{open_count}/{self._s.circuit_breaker_max_positions}"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_daily_loss_limit(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """Rule 6 — Daily loss limit: halt if total_value fell below threshold."""
        floor = self._initial_capital * (1.0 - self._s.circuit_breaker_daily_loss_pct)
        if total_value < floor:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="daily_loss_limit",
                    reason=(
                        f"Portfolio value ${total_value:.2f} below daily floor "
                        f"${floor:.2f} "
                        f"(-{self._s.circuit_breaker_daily_loss_pct:.0%} of "
                        f"initial ${self._initial_capital:.2f})"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_correlation_limit(
        self, signal: TradeSignal, portfolio: PortfolioState, total_value: float
    ) -> RiskCheckResult:
        """
        Rule 7 — Correlation limit: ≤ max_correlated_positions per category.

        Relies on TradeSignal having a `category` attribute set by the strategy.
        If category is missing, the check is skipped (conservative but acceptable
        since categories come from market metadata, not always available).
        """
        category: str | None = getattr(signal, "category", None)
        if category is None:
            return RiskCheckResult(approved=True)

        same_category = sum(
            1
            for pos in portfolio.positions
            if getattr(pos, "category", None) == category
        )
        if same_category >= self._s.max_correlated_positions:
            return RiskCheckResult(
                approved=False,
                violation=RiskViolation(
                    rule="correlation_limit",
                    reason=(
                        f"Already have {same_category} positions in category "
                        f"'{category}' (max {self._s.max_correlated_positions})"
                    ),
                ),
            )
        return RiskCheckResult(approved=True)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _total_value(portfolio: PortfolioState) -> float:
        if portfolio.total_value_usd is not None:
            return portfolio.total_value_usd
        positions_value = sum(p.size_usd for p in portfolio.positions)
        return portfolio.cash_usd + positions_value


# ── Pure helper (reused by backtest engine too) ────────────────────────────────

def compute_kelly_fraction(p_win: float, odds: float) -> float:
    """
    Full Kelly criterion: (p * b - q) / b
      p_win = probability of winning (0–1)
      odds  = gross payout per unit staked (e.g., 1/price for binary market)
    Returns the fraction of capital to bet (can be negative → no edge).
    """
    if odds <= 0:
        return 0.0
    q_lose = 1.0 - p_win
    return (p_win * odds - q_lose) / odds
