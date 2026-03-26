"""Polymarket CLOB API fetcher — price history and orderbook snapshots.

APIs used:
  - REST: https://clob.polymarket.com/prices-history  (price timeseries)
  - py-clob-client: ClobClient.get_order_book()       (current orderbook)

All public methods return Pydantic models — no raw dicts cross module boundaries.

CLI usage (inside bot container):
    python data/fetchers/clob_fetcher.py --token-id <id> --start 2024-01-01 --end 2024-06-01 --save
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import httpx
from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

from config.schemas import OrderLevel, OrderbookSnapshot, PricePoint


class CLOBFetcher:
    """Fetches price history and orderbook data from the Polymarket CLOB."""

    BASE_URL = "https://clob.polymarket.com"

    # Fidelity values accepted by the API (minutes per candle)
    FIDELITY_1MIN = 1
    FIDELITY_1H = 60
    FIDELITY_6H = 360
    FIDELITY_1D = 1440

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        clob_client: ClobClient | None = None,
    ) -> None:
        self._http = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )
        self._clob = clob_client or ClobClient(self.BASE_URL)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_price_history(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        fidelity: int = FIDELITY_1H,
    ) -> list[PricePoint]:
        """Fetch OHLC price history for a token.

        Args:
            token_id:  Polymarket YES or NO token ID.
            start_ts:  Unix timestamp (seconds) for range start.
            end_ts:    Unix timestamp (seconds) for range end.
            fidelity:  Candle resolution in minutes (1, 60, 360, 1440).

        Returns:
            List of PricePoint ordered by timestamp ascending.
        """
        logger.info(
            "Fetching price history token={} fidelity={}min start={} end={}",
            token_id[:16] + "…",
            fidelity,
            datetime.fromtimestamp(start_ts, tz=timezone.utc).date(),
            datetime.fromtimestamp(end_ts, tz=timezone.utc).date(),
        )

        params = {
            "token_id": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }

        try:
            response = self._http.get("/prices-history", params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("CLOB prices-history HTTP {}: {}", exc.response.status_code, token_id)
            raise
        except httpx.RequestError as exc:
            logger.error("CLOB prices-history request error: {}", exc)
            raise

        history = data.get("history", [])
        points = [_parse_price_point(token_id, row) for row in history]
        logger.info("Price history fetched: {} points for token {}", len(points), token_id[:16] + "…")
        return points

    def get_orderbook(self, token_id: str, top_n: int = 5) -> OrderbookSnapshot:
        """Fetch the current live orderbook for a token.

        Args:
            token_id: Polymarket YES or NO token ID.
            top_n:    Keep only the top N bid and ask levels.

        Returns:
            OrderbookSnapshot with bids/asks, mid_price, and spread.
        """
        logger.debug("Fetching orderbook for token {}", token_id[:16] + "…")

        book = self._clob.get_order_book(token_id)
        now = datetime.now(timezone.utc)

        bids = sorted(
            [OrderLevel(price=float(b.price), size=float(b.size)) for b in (book.bids or [])],
            key=lambda x: x.price,
            reverse=True,
        )[:top_n]

        asks = sorted(
            [OrderLevel(price=float(a.price), size=float(a.size)) for a in (book.asks or [])],
            key=lambda x: x.price,
        )[:top_n]

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        spread = (best_ask - best_bid) if best_bid and best_ask else None

        return OrderbookSnapshot(
            token_id=token_id,
            timestamp=now,
            bids=bids,
            asks=asks,
            mid_price=mid_price,
            spread=spread,
        )

    def get_orderbooks_batch(self, token_ids: list[str], top_n: int = 5) -> list[OrderbookSnapshot]:
        """Fetch orderbooks for multiple tokens in a single request."""
        logger.info("Fetching orderbooks batch: {} tokens", len(token_ids))
        params = [BookParams(token_id=tid) for tid in token_ids]
        books = self._clob.get_order_books(params)
        now = datetime.now(timezone.utc)

        snapshots: list[OrderbookSnapshot] = []
        for book in books:
            bids = sorted(
                [OrderLevel(price=float(b.price), size=float(b.size)) for b in (book.bids or [])],
                key=lambda x: x.price,
                reverse=True,
            )[:top_n]
            asks = sorted(
                [OrderLevel(price=float(a.price), size=float(a.size)) for a in (book.asks or [])],
                key=lambda x: x.price,
            )[:top_n]

            best_bid = bids[0].price if bids else None
            best_ask = asks[0].price if asks else None
            mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else None
            spread = (best_ask - best_bid) if best_bid and best_ask else None

            snapshots.append(OrderbookSnapshot(
                token_id=book.asset_id or "",
                timestamp=now,
                bids=bids,
                asks=asks,
                mid_price=mid_price,
                spread=spread,
            ))

        return snapshots


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _parse_price_point(token_id: str, row: dict) -> PricePoint:
    """Map a single history row {t, p} to a PricePoint. Pure function."""
    ts = datetime.fromtimestamp(int(row["t"]), tz=timezone.utc)
    price = float(row["p"])
    # Clamp to [0, 1] — API occasionally returns values like 1.0001 due to rounding
    price = max(0.0, min(1.0, price))
    return PricePoint(token_id=token_id, timestamp=ts, price=price)


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    from datetime import date
    parser = argparse.ArgumentParser(description="Fetch CLOB price history")
    parser.add_argument("--token-id", required=True, help="YES or NO token ID")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=str(date.today()), help="End date YYYY-MM-DD")
    parser.add_argument(
        "--fidelity", type=int, default=60,
        help="Candle resolution in minutes (1, 60, 360, 1440)"
    )
    parser.add_argument("--save", action="store_true", help="Persist to PostgreSQL")
    return parser.parse_args()


async def _save_prices(prices: list[PricePoint]) -> None:
    from data.db import AsyncSessionFactory
    from data.repository import upsert_prices

    async with AsyncSessionFactory() as session:
        n = await upsert_prices(session, prices)
    logger.info("Saved {} price points to DB", n)


if __name__ == "__main__":
    args = _parse_args()

    start_ts = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp())

    fetcher = CLOBFetcher()
    prices = fetcher.get_price_history(
        token_id=args.token_id,
        start_ts=start_ts,
        end_ts=end_ts,
        fidelity=args.fidelity,
    )

    logger.info("Fetched {} price points", len(prices))

    if args.save:
        asyncio.run(_save_prices(prices))
