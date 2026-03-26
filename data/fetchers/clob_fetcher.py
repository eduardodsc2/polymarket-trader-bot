"""
Polymarket CLOB API fetcher — prices, orderbook.

API: https://clob.polymarket.com

Status: stub — to be implemented in Phase 1.
"""
from __future__ import annotations

from config.schemas import OrderbookSnapshot, PricePoint


class CLOBFetcher:
    BASE_URL = "https://clob.polymarket.com"

    def get_price_history(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        interval: str = "1h",
    ) -> list[PricePoint]:
        raise NotImplementedError("CLOBFetcher will be implemented in Phase 1")

    def get_orderbook(self, token_id: str) -> OrderbookSnapshot:
        raise NotImplementedError("CLOBFetcher will be implemented in Phase 1")
