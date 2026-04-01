"""
Pydantic v2 schemas shared across the entire project.

All data flowing between layers (fetchers → DB, strategies → executor, etc.)
is validated through these models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── Market ────────────────────────────────────────────────────────────────────

class Market(BaseModel):
    condition_id: str
    question: str
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    resolved: bool = False
    outcome: Optional[str] = None
    volume_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    fetched_at: Optional[datetime] = None

    # YES and NO token IDs (from CLOB API)
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None


# ── Price ─────────────────────────────────────────────────────────────────────

class PricePoint(BaseModel):
    token_id: str
    timestamp: datetime
    price: float = Field(..., ge=0.0, le=1.0)
    volume: Optional[float] = None

    @field_validator("price")
    @classmethod
    def price_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Price must be between 0 and 1, got {v}")
        return v


# ── Orderbook ─────────────────────────────────────────────────────────────────

class OrderLevel(BaseModel):
    price: float = Field(..., ge=0.0, le=1.0)
    size: float = Field(..., ge=0.0)


class OrderbookSnapshot(BaseModel):
    token_id: str
    timestamp: datetime
    bids: list[OrderLevel] = Field(default_factory=list)
    asks: list[OrderLevel] = Field(default_factory=list)
    mid_price: Optional[float] = None
    spread: Optional[float] = None


# ── Trade ─────────────────────────────────────────────────────────────────────

class Trade(BaseModel):
    strategy: str
    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    size_usd: float = Field(..., ge=0)
    price: float = Field(..., ge=0.0, le=1.0)
    fee_usd: float = 0.0
    mode: Literal["backtest", "paper", "live"]
    executed_at: datetime


# ── Signal ────────────────────────────────────────────────────────────────────

class TradeSignal(BaseModel):
    """Output from a strategy: intent to trade a specific token."""
    strategy: str
    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    estimated_probability: float = Field(..., ge=0.0, le=1.0)
    market_price: float = Field(..., ge=0.0, le=1.0)
    edge: float                  # estimated_probability - market_price (for BUY)
    suggested_size_usd: float = Field(..., gt=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: Optional[str] = None


# ── News ─────────────────────────────────────────────────────────────────────

class NewsArticle(BaseModel):
    source: str
    title: str
    body: Optional[str] = None
    url: Optional[str] = None
    published_at: datetime
    fetched_at: Optional[datetime] = None
    relevance_score: float = 0.0


class SentimentReading(BaseModel):
    """Social sentiment data point — from LunarCrush or VADER."""
    topic: str
    source: Literal["lunarcrush", "vader"]
    sentiment: float = Field(..., ge=-1.0, le=1.0)
    posts_active: Optional[int] = None
    interactions: Optional[int] = None
    galaxy_score: Optional[float] = None
    timestamp: datetime


class NewsFeatures(BaseModel):
    condition_id: str
    timestamp: datetime
    article_count_24h: Optional[int] = None
    article_count_delta: Optional[float] = None
    avg_sentiment_score: Optional[float] = None
    sentiment_std: Optional[float] = None
    sentiment_delta_24h: Optional[float] = None
    price_vs_sentiment_gap: Optional[float] = None


# ── LLM ──────────────────────────────────────────────────────────────────────

class LLMEstimate(BaseModel):
    condition_id: str
    model: str
    prompt_hash: str
    probability: float = Field(..., ge=0.0, le=1.0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reasoning: Optional[str] = None
    sources: Optional[list[str]] = None


# ── Portfolio ─────────────────────────────────────────────────────────────────

class Position(BaseModel):
    condition_id: str
    token_id: str
    strategy: str
    side: Literal["BUY", "SELL"]
    size_usd: float
    entry_price: float
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    opened_at: datetime


class PortfolioState(BaseModel):
    cash_usd: float
    positions: list[Position] = Field(default_factory=list)
    realized_pnl: float = 0.0
    total_value_usd: Optional[float] = None
    timestamp: Optional[datetime] = None


# ── Order lifecycle ───────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    """Concrete order emitted by a strategy and processed by the fill model."""
    order_id: str
    strategy: str
    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    size_usd: float = Field(..., gt=0)
    limit_price: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    timestamp: datetime
    edge: float = 0.0  # Estimated edge for risk checks (spread for MarketMaker)


class OrderFill(BaseModel):
    """Result returned by the fill model after processing an OrderRequest."""
    order_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    requested_size_usd: float
    filled_size_usd: float
    fill_price: float = Field(..., ge=0.0, le=1.0)
    slippage_bps: float = 0.0
    fee_usd: float = 0.0
    timestamp: datetime
    partial: bool = False


# ── Portfolio snapshot ─────────────────────────────────────────────────────────

class PortfolioSnapshot(BaseModel):
    """Point-in-time portfolio capture — used to build the equity curve."""
    timestamp: datetime
    cash_usd: float
    positions_value_usd: float
    total_value_usd: float
    unrealized_pnl: float
    realized_pnl: float
    open_positions: int


# ── Backtest metrics ──────────────────────────────────────────────────────────

class BacktestMetrics(BaseModel):
    strategy: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_capital: float
    total_return_pct: float
    cagr: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    win_rate: float
    total_trades: int
    brier_score: Optional[float] = None
    expected_value_per_trade: Optional[float] = None


# ── On-chain reconciliation ───────────────────────────────────────────────────

class ReconciliationReport(BaseModel):
    """Result of Blockscout on-chain audit for a wallet."""
    wallet_address: str
    chain_id: int = 137
    checked_at: datetime
    onchain_usdc_balance: float
    internal_cash_balance: float
    balance_discrepancy: float          # onchain - internal; flag if abs > 0.10
    unrecorded_transfers: list[str] = Field(default_factory=list)   # tx hashes
    unconfirmed_tx_hashes: list[str] = Field(default_factory=list)  # open positions not yet confirmed
    ok: bool                            # True if no discrepancies detected
