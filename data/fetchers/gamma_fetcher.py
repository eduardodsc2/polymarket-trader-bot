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

    def get_active_markets(
        self,
        min_volume: float = 10_000.0,
        max_markets: int = 2_000,
    ) -> list[Market]:
        """Fetch active (unresolved) markets above min_volume.

        Args:
            min_volume:  Client-side volume filter in USD.
            max_markets: Hard pagination cap to avoid fetching the entire
                         10k+ active-market catalogue (default 2 000).
        """
        logger.info("Fetching active markets (min_volume={}, cap={})", min_volume, max_markets)
        extra: dict[str, Any] = {}
        if min_volume > 0:
            extra["volume_num_min"] = min_volume
        markets = self._fetch_all_pages(closed=False, extra_params=extra or None, max_markets=max_markets)
        filtered = [m for m in markets if (m.volume_usd or 0) >= min_volume]
        logger.info("Active markets fetched: {} total, {} above min_volume", len(markets), len(filtered))
        return filtered

    def get_short_window_markets(
        self,
        max_hours: float = 2.0,
        max_markets: int = 500,
    ) -> list[Market]:
        """Fetch active markets resolving within the next ``max_hours`` hours.

        Skips volume filtering — short-window markets accumulate little volume
        per market but are valid trade targets. Uses server-side end_date_max
        to avoid paginating the full active catalogue.

        Args:
            max_hours:   Upper bound on hours to resolution (e.g. 2.0).
            max_markets: Pagination cap (default 500).
        """
        now = datetime.now(timezone.utc)
        end_max = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        # Add max_hours to now for the upper bound
        from datetime import timedelta
        end_max = (now + timedelta(hours=max_hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        logger.info(
            "Fetching short-window markets (resolving within {}h, end_max={})",
            max_hours, end_max,
        )
        extra: dict[str, Any] = {
            "end_date_min": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "end_date_max": end_max,
        }
        markets = self._fetch_all_pages(closed=False, extra_params=extra, max_markets=max_markets)
        logger.info("Short-window markets fetched: {} raw", len(markets))
        return markets

    def get_resolved_markets(
        self,
        start_date: str,
        end_date: str,
        min_volume: float = 0.0,
        max_markets: int = 2_000,
    ) -> list[Market]:
        """Fetch resolved markets with end_date between start_date and end_date.

        Uses server-side date and volume filters (end_date_min, end_date_max,
        volume_num_min) to avoid fetching the entire 650k+ market history.
        A client-side pass is still applied as a safety net.

        Args:
            start_date:  ISO date string, e.g. '2024-01-01'
            end_date:    ISO date string, e.g. '2024-12-31'
            min_volume:  Only return markets above this USDC volume (default 0).
            max_markets: Pagination cap — stop after fetching this many raw records
                         to avoid runaway pagination (default 2 000).
        """
        logger.info(
            "Fetching resolved markets between {} and {} (min_volume={}, cap={})",
            start_date, end_date, min_volume, max_markets,
        )

        # Build server-side filter params (Gamma API date-time format).
        # negRisk=false excludes multi-outcome group sub-markets whose CLOB
        # token IDs are not supported by the prices-history endpoint.
        extra: dict[str, Any] = {
            "end_date_min": f"{start_date}T00:00:00Z",
            "end_date_max": f"{end_date}T23:59:59Z",
            "negRisk": "false",
        }
        if min_volume > 0:
            extra["volume_num_min"] = min_volume

        markets = self._fetch_all_pages(closed=True, extra_params=extra, max_markets=max_markets)

        # Client-side safety net — normalise datetimes to UTC.
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

        def _utc(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        filtered = [
            m for m in markets
            if m.end_date is not None
            and start <= _utc(m.end_date) <= end_dt
            and (m.volume_usd or 0.0) >= min_volume
        ]
        logger.info(
            "Resolved markets after filter: {} / {} raw records",
            len(filtered), len(markets),
        )
        return filtered

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_all_pages(
        self,
        closed: bool,
        extra_params: dict[str, Any] | None = None,
        max_markets: int = 100_000,
    ) -> list[Market]:
        """Paginate through the /markets endpoint using offset+limit.

        Args:
            closed:      True for resolved markets, False for active.
            extra_params: Additional query parameters merged into each request.
            max_markets: Hard stop — return early once this many raw records
                         have been collected.  Prevents runaway pagination on
                         large result sets (the Gamma API has 650 k+ records).
        """
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
            if not isinstance(batch_raw, list):
                break
            if not batch_raw:
                break

            batch = [self._parse_market(m) for m in batch_raw]
            markets.extend(batch)
            offset += len(batch_raw)

            logger.debug("Fetched {} markets so far (offset={})", len(markets), offset)

            if len(batch_raw) < self._PAGE_SIZE:
                break

            if len(markets) >= max_markets:
                logger.info("Reached max_markets cap ({}) — stopping pagination.", max_markets)
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
        # clobTokenIds may arrive as a JSON string or already parsed list
        import json as _json
        raw_ids = raw.get("clobTokenIds") or []
        if isinstance(raw_ids, str):
            try:
                raw_ids = _json.loads(raw_ids)
            except (ValueError, TypeError):
                raw_ids = []
        clob_ids: list[str] = raw_ids

        raw_outcomes = raw.get("outcomes") or []
        if isinstance(raw_outcomes, str):
            try:
                raw_outcomes = _json.loads(raw_outcomes)
            except (ValueError, TypeError):
                raw_outcomes = []
        outcomes: list[str] = raw_outcomes
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
            category=raw.get("category") or _infer_category(raw),
            end_date=end_date,
            resolved=bool(raw.get("closed", False)),
            outcome=raw.get("outcome"),
            volume_usd=_to_float(raw.get("volume")),
            liquidity_usd=_to_float(raw.get("liquidity")),
            fetched_at=datetime.now(timezone.utc),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )


def _infer_category(raw: dict[str, Any]) -> str | None:
    """Infer market category from Gamma API fields when 'category' is absent."""
    if raw.get("sportsMarketType"):
        return "sports"
    question = (raw.get("question") or "").lower()
    if any(w in question for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "price of", "above $", "below $"]):
        return "crypto"
    if any(w in question for w in ["election", "president", "congress", "senate", "vote", "democrat", "republican", "trump", "biden", "harris", "governor", "mayor", "parliament", "minister"]):
        return "politics"
    if any(w in question for w in ["fed ", "interest rate", "gdp", "recession", "cpi", "inflation", "unemployment", "dow jones", "s&p", "nasdaq"]):
        return "finance"
    if any(w in question for w in ["ceasefire", "war", "attack", "arrest", "indicted", "convicted", "resign", "assassin", "hurricane", "earthquake", "disaster"]):
        return "news"
    if any(w in question for w in ["ai ", "gpt", "claude", "gemini", "openai", "anthropic", "llm", "model release", "artificial intelligence"]):
        return "science"
    return None


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
    parser.add_argument("--max-markets", type=int, default=2_000, help="Pagination cap for resolved fetch")
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
        result = fetcher.get_resolved_markets(
            args.start, args.end,
            min_volume=args.min_volume,
            max_markets=args.max_markets,
        )
    else:
        result = fetcher.get_active_markets(min_volume=args.min_volume)

    logger.info("Total markets fetched: {}", len(result))

    if args.save:
        asyncio.run(_save(result))
