"""
Polymarket Gamma API fetcher — market metadata.

API: https://gamma-api.polymarket.com/markets

Status: stub — to be implemented in Phase 1.
"""
from __future__ import annotations

from config.schemas import Market


class GammaFetcher:
    BASE_URL = "https://gamma-api.polymarket.com"

    def get_active_markets(self, min_volume: float = 10_000.0) -> list[Market]:
        raise NotImplementedError("GammaFetcher will be implemented in Phase 1")

    def get_resolved_markets(self, start_date: str, end_date: str) -> list[Market]:
        raise NotImplementedError("GammaFetcher will be implemented in Phase 1")
