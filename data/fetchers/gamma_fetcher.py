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

        Uses server-side date filtering (end_date_min / end_date_max) to avoid
        downloading the entire history.

        Args:
            start_date: ISO date string, e.g. '2024-01-01'
            end_date:   ISO date string, e.g. '2024-12-31'
        """
        logger.info("Fetching resolved markets between {} and {}", start_date, end_date)
        extra = {"end_date_min": start_date, "end_date_max": end_date}
        markets = self._fetch_all_pages(closed=True, extra_params=extra)
        logger.info("Resolved markets in range: {}", len(markets))
        return markets

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_all_pages(
        self,
        closed: bool,
        extra_params: dict[str, Any] | None = None,
    ) -> list[Market]:
        """Paginate through the /markets endpoint using offset+limit."""
        markets: list[Market] = []
        offset = 0

        while True:
            params: dict[str, Any] = {
                "limit": self._PAGE_SIZE,
                "closed": str(closed).lower(),
                "offset": offset,
            }
            if extra_params:
                params.update(extra_params)

            batch_raw = self._get("/markets", params=params)
            # API returns a list directly
            if not isinstance(batch_raw, list):
                break
            if not batch_raw:
                break

            batch = [self._parse_market(m) for m in batch_raw]
            markets.extend(batch)
            offset += len(batch_raw)

            logger.debug("Fetched {} markets so far (offset={})", len(markets), offset)

            # If we got fewer items than the page size, we've reached the end
            if len(batch_raw) < self._PAGE_SIZE:
                break

        return markets

    def _get(self, path: str, params: dict[str, Any] | None = None) -> list | dict:
        """Make a GET request; raise on HTTP errors. Returns list or dict."""
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
        """Map a raw Gamma API market dict to a Market Pydantic model.

        Actual field names from the Gamma API (camelCase):
          conditionId, endDateIso, clobTokenIds, outcomes, closed, active,
          volume, liquidity, category
        """
        # Token IDs: clobTokenIds[0] = YES, clobTokenIds[1] = NO
        # (matches outcomes[0] = "Yes", outcomes[1] = "No")
        clob_ids: list[str] = raw.get("clobTokenIds") or []
        outcomes: list[str] = raw.get("outcomes") or []
        yes_token_id: str | None = None
        no_token_id: str | None = None
        for idx, outcome in enumerate(outcomes):
            if idx < len(clob_ids):
                if outcome.upper() == "YES":
                    yes_token_id = clob_ids[idx]
                elif outcome.upper() == "NO":
                    no_token_id = clob_ids[idx]
        # Fallback: if outcomes are ["Yes","No"] positionally
        if yes_token_id is None and len(clob_ids) >= 1:
            yes_token_id = clob_ids[0]
        if no_token_id is None and len(clob_ids) >= 2:
            no_token_id = clob_ids[1]

        # Parse end_date
        end_date: datetime | None = None
        raw_end = raw.get("endDateIso") or raw.get("endDate") or raw.get("end_date_iso")
        if raw_end:
            try:
                end_date = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return Market(
            condition_id=raw.get("conditionId") or raw.get("condition_id", ""),
            question=raw.get("question", ""),
            category=raw.get("category"),
            end_date=end_date,
            resolved=bool(raw.get("closed", False)),
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
