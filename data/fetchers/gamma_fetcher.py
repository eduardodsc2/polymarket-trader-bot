"""Polymarket Gamma API fetcher — market metadata.

API: https://gamma-api.polymarket.com/markets

Fetches market metadata with pagination support. All public methods return
Pydantic models — no raw dicts cross module boundaries.

CLI usage (inside bot container):
    python data/fetchers/gamma_fetcher.py --min-volume 10000 --save
    python data/fetchers/gamma_fetcher.py --resolved --start 2024-01-01 --end 2024-12-31 --save
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from config.schemas import Market
from config.settings import settings


class GammaFetcher:
    """Fetches market metadata from the Polymarket Gamma API."""

    BASE_URL = "https://gamma-api.polymarket.com"
    _PAGE_SIZE = 100

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._client = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_active_markets(self, min_volume: float = 10_000.0) -> list[Market]:
        """Fetch all currently active (unresolved) markets above min_volume."""
        logger.info("Fetching active markets (min_volume={})", min_volume)
        markets = self._fetch_all_pages(closed=False)
        filtered = [m for m in markets if (m.volume_usd or 0) >= min_volume]
        logger.info("Active markets fetched: {} total, {} above min_volume", len(markets), len(filtered))
        return filtered

    def get_resolved_markets(self, start_date: str, end_date: str) -> list[Market]:
        """Fetch all resolved markets with end_date between start_date and end_date.

        Args:
            start_date: ISO date string, e.g. '2024-01-01'
            end_date:   ISO date string, e.g. '2024-12-31'
        """
        logger.info("Fetching resolved markets between {} and {}", start_date, end_date)
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

        markets = self._fetch_all_pages(closed=True)
        filtered = [
            m for m in markets
            if m.resolved
            and m.end_date is not None
            and start_dt <= m.end_date <= end_dt
        ]
        logger.info("Resolved markets in range: {}", len(filtered))
        return filtered

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_all_pages(self, closed: bool) -> list[Market]:
        """Paginate through the /markets endpoint and collect all markets."""
        markets: list[Market] = []
        cursor: str | None = None

        while True:
            params: dict[str, Any] = {
                "limit": self._PAGE_SIZE,
                "closed": str(closed).lower(),
            }
            if cursor:
                params["next_cursor"] = cursor

            raw = self._get("/markets", params=params)
            batch = [self._parse_market(m) for m in raw.get("data", [])]
            markets.extend(batch)

            cursor = raw.get("next_cursor")
            if not cursor or cursor == "LTE=":
                break

            logger.debug("Fetched {} markets, cursor={}", len(markets), cursor)

        return markets

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request; raise on HTTP errors."""
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("Gamma API HTTP error {}: {}", exc.response.status_code, path)
            raise
        except httpx.RequestError as exc:
            logger.error("Gamma API request error: {}", exc)
            raise

    @staticmethod
    def _parse_market(raw: dict[str, Any]) -> Market:
        """Map a raw Gamma API market dict to a Market Pydantic model."""
        # Extract YES/NO token IDs from the tokens array
        yes_token_id: str | None = None
        no_token_id: str | None = None
        for token in raw.get("tokens", []):
            outcome = (token.get("outcome") or "").upper()
            if outcome == "YES":
                yes_token_id = token.get("token_id")
            elif outcome == "NO":
                no_token_id = token.get("token_id")

        # Parse end_date (API returns ISO string or None)
        end_date: datetime | None = None
        raw_end = raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("end_date")
        if raw_end:
            try:
                end_date = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return Market(
            condition_id=raw["condition_id"],
            question=raw.get("question", ""),
            category=raw.get("category") or raw.get("tag"),
            end_date=end_date,
            resolved=bool(raw.get("closed", False) and raw.get("resolution_source")),
            outcome=raw.get("outcome"),
            volume_usd=_to_float(raw.get("volume")),
            liquidity_usd=_to_float(raw.get("liquidity")),
            fetched_at=datetime.now(timezone.utc),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )


def _to_float(value: Any) -> float | None:
    """Safely coerce a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Polymarket market metadata")
    parser.add_argument("--min-volume", type=float, default=settings.min_market_volume_usd)
    parser.add_argument("--resolved", action="store_true", help="Fetch resolved markets")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2024-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--save", action="store_true", help="Persist to PostgreSQL")
    return parser.parse_args()


async def _save(markets: list[Market]) -> None:
    from data.db import AsyncSessionFactory
    from data.repository import upsert_markets

    async with AsyncSessionFactory() as session:
        n = await upsert_markets(session, markets)
    logger.info("Saved {} markets to DB", n)


if __name__ == "__main__":
    args = _parse_args()
    fetcher = GammaFetcher()

    if args.resolved:
        result = fetcher.get_resolved_markets(args.start, args.end)
    else:
        result = fetcher.get_active_markets(min_volume=args.min_volume)

    logger.info("Total markets fetched: {}", len(result))

    if args.save:
        asyncio.run(_save(result))
