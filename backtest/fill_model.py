"""
Order fill simulation: slippage and partial fills.

Polymarket fee model (per PLAN.md): 2% of gross payout at resolution,
NOT at trade time. This module applies only market-impact slippage.
Fee at resolution is handled by Portfolio.resolve_position().

Usage:
    fill_model = FillModel(slippage_bps=10)
    fill = fill_model.simulate_market_buy("tok_yes", size_usd=100, price=0.60, timestamp=ts)
    fill = fill_model.simulate_limit_buy("tok_yes", size_usd=100, limit_price=0.58,
                                          current_price=0.57, timestamp=ts)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from config.schemas import OrderFill, OrderRequest


class FillModel:
    """
    Simulates realistic order execution for a prediction market CLOB.

    Slippage is applied as a fixed cost in basis points (bps) on the mid price,
    modelling market-order impact on a thin orderbook. Limit orders fill only
    when the market price crosses the limit — no partial fill at the limit level
    (binary prediction markets rarely have deep L2 depth in historical data).
    """

    def __init__(self, slippage_bps: int = 10) -> None:
        """
        Args:
            slippage_bps: One-way slippage in basis points applied to market
                          orders (default 10 bps = 0.10%).
        """
        self.slippage_bps = slippage_bps
        self._slippage_factor = slippage_bps / 10_000

    # ── Public API ─────────────────────────────────────────────────────────────

    def simulate_market_buy(
        self,
        token_id: str,
        size_usd: float,
        price: float,
        timestamp: datetime,
        order_id: str | None = None,
    ) -> OrderFill:
        """
        Fill a market BUY at price + slippage, capped at 1.0.

        Args:
            token_id:   YES or NO token being bought.
            size_usd:   USDC amount to spend.
            price:      Current mid price (0–1).
            timestamp:  Event timestamp.
            order_id:   Optional stable ID; generated if None.
        """
        fill_price = min(1.0, price * (1 + self._slippage_factor))
        return OrderFill(
            order_id=order_id or str(uuid.uuid4()),
            token_id=token_id,
            side="BUY",
            requested_size_usd=size_usd,
            filled_size_usd=size_usd,
            fill_price=fill_price,
            slippage_bps=float(self.slippage_bps),
            fee_usd=0.0,  # fees applied at resolution
            timestamp=timestamp,
            partial=False,
        )

    def simulate_market_sell(
        self,
        token_id: str,
        size_usd: float,
        price: float,
        timestamp: datetime,
        order_id: str | None = None,
    ) -> OrderFill:
        """
        Fill a market SELL at price - slippage, floored at 0.0.

        Args:
            token_id:   YES or NO token being sold.
            size_usd:   Notional USDC value to sell.
            price:      Current mid price (0–1).
            timestamp:  Event timestamp.
            order_id:   Optional stable ID; generated if None.
        """
        fill_price = max(0.0, price * (1 - self._slippage_factor))
        return OrderFill(
            order_id=order_id or str(uuid.uuid4()),
            token_id=token_id,
            side="SELL",
            requested_size_usd=size_usd,
            filled_size_usd=size_usd,
            fill_price=fill_price,
            slippage_bps=float(self.slippage_bps),
            fee_usd=0.0,
            timestamp=timestamp,
            partial=False,
        )

    def simulate_limit_buy(
        self,
        token_id: str,
        size_usd: float,
        limit_price: float,
        current_price: float,
        timestamp: datetime,
        order_id: str | None = None,
    ) -> OrderFill | None:
        """
        Fill a limit BUY if current_price <= limit_price.

        Returns None if the limit has not been reached.
        Fills at the limit_price (not current_price) — conservative assumption.
        """
        if current_price > limit_price:
            return None
        return OrderFill(
            order_id=order_id or str(uuid.uuid4()),
            token_id=token_id,
            side="BUY",
            requested_size_usd=size_usd,
            filled_size_usd=size_usd,
            fill_price=limit_price,
            slippage_bps=0.0,
            fee_usd=0.0,
            timestamp=timestamp,
            partial=False,
        )

    def simulate_limit_sell(
        self,
        token_id: str,
        size_usd: float,
        limit_price: float,
        current_price: float,
        timestamp: datetime,
        order_id: str | None = None,
    ) -> OrderFill | None:
        """
        Fill a limit SELL if current_price >= limit_price.

        Returns None if the limit has not been reached.
        """
        if current_price < limit_price:
            return None
        return OrderFill(
            order_id=order_id or str(uuid.uuid4()),
            token_id=token_id,
            side="SELL",
            requested_size_usd=size_usd,
            filled_size_usd=size_usd,
            fill_price=limit_price,
            slippage_bps=0.0,
            fee_usd=0.0,
            timestamp=timestamp,
            partial=False,
        )

    def process_order_request(
        self,
        request: OrderRequest,
        current_price: float,
    ) -> OrderFill | None:
        """
        Route an OrderRequest to the appropriate fill simulation.

        Market orders (limit_price=None) always fill.
        Limit orders fill only when the market price crosses the limit.
        Returns None for unfilled limit orders.
        """
        if request.limit_price is None:
            # Market order
            if request.side == "BUY":
                return self.simulate_market_buy(
                    request.token_id, request.size_usd, current_price, request.timestamp,
                    order_id=request.order_id,
                )
            else:
                return self.simulate_market_sell(
                    request.token_id, request.size_usd, current_price, request.timestamp,
                    order_id=request.order_id,
                )
        else:
            # Limit order
            if request.side == "BUY":
                return self.simulate_limit_buy(
                    request.token_id, request.size_usd, request.limit_price,
                    current_price, request.timestamp, order_id=request.order_id,
                )
            else:
                return self.simulate_limit_sell(
                    request.token_id, request.size_usd, request.limit_price,
                    current_price, request.timestamp, order_id=request.order_id,
                )
