"""
Backtest event types.

Events are immutable dataclasses. The engine wraps each in a _QueueEntry
(timestamp, seq, event) tuple for heapq ordering — never compare events directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Union


@dataclass(frozen=True)
class PriceUpdateEvent:
    """Emitted for every price tick in the backtest."""
    timestamp: datetime
    token_id: str
    price: float
    bid: float | None = None
    ask: float | None = None
    condition_id: str = ""


@dataclass(frozen=True)
class MarketResolutionEvent:
    """Emitted when a market closes and an outcome is known."""
    timestamp: datetime
    condition_id: str
    outcome: str            # "YES" or "NO"
    yes_token_id: str = ""
    no_token_id: str = ""


@dataclass(frozen=True)
class OrderFillEvent:
    """Emitted by the engine after the fill model processes an OrderRequest."""
    timestamp: datetime
    order_id: str
    token_id: str
    side: str               # "BUY" | "SELL"
    price: float
    size_usd: float
    fee_usd: float = 0.0


# Union type for the event queue
BacktestEvent = Union[PriceUpdateEvent, MarketResolutionEvent, OrderFillEvent]
