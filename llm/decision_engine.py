"""
Compare LLM probability estimate to market price → trade signal.

All functions are pure — no I/O, no side effects.
"""
from __future__ import annotations

from config.schemas import LLMEstimate, TradeSignal
from config.settings import settings


# ── Pure computation functions ─────────────────────────────────────────────────

def compute_edge(
    llm_probability: float,
    market_price: float,
    confidence: float = 1.0,
) -> float:
    """Compute the estimated edge between LLM probability and market price.

    Edge is positive when we think the market underprices YES,
    negative when the market overprices YES.

    The raw edge is scaled by confidence to be conservative when uncertain.

    Args:
        llm_probability: Estimated probability from LLM (0.0–1.0).
        market_price: Current YES token price (0.0–1.0).
        confidence: LLM confidence level (0.0–1.0); scales down raw edge.

    Returns:
        Confidence-weighted edge in [-1, +1].
    """
    raw_edge = llm_probability - market_price
    return round(raw_edge * confidence, 4)


def should_trade(
    edge: float,
    min_edge: float | None = None,
    confidence: float = 1.0,
    confidence_threshold: float = 0.33,
) -> bool:
    """Return True if the edge is large enough to justify a trade.

    Args:
        edge: Signed edge value from compute_edge().
        min_edge: Minimum absolute edge to trade (defaults to settings.min_edge_pct).
        confidence: LLM confidence level; skips LOW confidence trades.
        confidence_threshold: Minimum confidence to proceed (default: 0.33 = LOW).
    """
    threshold = min_edge if min_edge is not None else settings.min_edge_pct
    if confidence < confidence_threshold:
        return False
    return abs(edge) >= threshold


def compute_kelly_size(
    edge: float,
    market_price: float,
    capital_usd: float,
    kelly_fraction: float | None = None,
) -> float:
    """Compute fractional Kelly position size in USD.

    Kelly formula for binary markets:
        f* = (p * b - q) / b
    where:
        p = probability of winning (LLM estimate)
        q = 1 - p (probability of losing)
        b = net odds = (1 - price) / price  [payout per $1 risked]

    Args:
        edge: Signed edge. Positive = BUY YES, negative = BUY NO.
        market_price: Current YES price (0.0–1.0).
        capital_usd: Total available capital in USD.
        kelly_fraction: Fractional Kelly multiplier (default: settings.kelly_fraction = 0.25).

    Returns:
        Recommended position size in USD (>= 0). Returns 0 if Kelly is negative.
    """
    frac = kelly_fraction if kelly_fraction is not None else settings.kelly_fraction

    if edge > 0:
        # BUY YES: price is the cost, payout is 1.0
        p_win = market_price + edge / max(1.0 - market_price, 1e-9)
        p_win = min(p_win, 0.99)  # clamp
        odds = (1.0 - market_price) / max(market_price, 1e-9)
    else:
        # BUY NO: (1 - price) is the cost, payout is 1.0
        no_price = 1.0 - market_price
        p_win = no_price + (-edge) / max(market_price, 1e-9)
        p_win = min(p_win, 0.99)
        odds = market_price / max(no_price, 1e-9)

    q_lose = 1.0 - p_win
    full_kelly = (p_win * odds - q_lose) / max(odds, 1e-9)

    if full_kelly <= 0:
        return 0.0

    size = capital_usd * full_kelly * frac
    # Cap at max_position_pct of capital
    max_size = capital_usd * settings.circuit_breaker_max_position_pct
    return round(min(size, max_size), 2)


# ── DecisionEngine class ────────────────────────────────────────────────────────

class DecisionEngine:
    """Converts an LLMEstimate + market price into a TradeSignal (or None).

    Injectable dependencies: min_edge, kelly_fraction, capital (passed per-call).
    """

    def decide(
        self,
        estimate: LLMEstimate,
        market_price: float,
        condition_id: str,
        token_id: str = "",
        capital_usd: float = 0.0,
    ) -> TradeSignal | None:
        """Evaluate an LLM estimate and return a TradeSignal if edge is sufficient.

        Args:
            estimate: LLMEstimate from the LLMEstimator.
            market_price: Current YES token price.
            condition_id: Market condition ID.
            token_id: YES or NO token ID to trade.
            capital_usd: Current available capital (used for Kelly sizing).

        Returns:
            TradeSignal if edge >= min_edge and confidence >= threshold.
            None if no trade is warranted.
        """
        confidence = estimate.confidence or 0.0
        edge = compute_edge(estimate.probability, market_price, confidence)

        if not should_trade(edge, confidence=confidence):
            return None

        side = "BUY" if edge > 0 else "SELL"
        size = compute_kelly_size(edge, market_price, capital_usd) if capital_usd > 0 else 10.0

        if size <= 0:
            return None

        return TradeSignal(
            strategy="value_betting",
            condition_id=condition_id,
            token_id=token_id,
            side=side,
            estimated_probability=estimate.probability,
            market_price=market_price,
            edge=edge,
            suggested_size_usd=size,
            confidence=confidence,
            reasoning=estimate.reasoning,
        )
