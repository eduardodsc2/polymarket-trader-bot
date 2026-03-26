"""pmxt archive downloader — free hourly Parquet orderbook snapshots.

Archive: https://archive.pmxt.dev

The pmxt archive provides hourly Parquet files with historical orderbook depth
data. Each file covers one hour and contains columns:
  market_id, token_id, timestamp, bid_price_1..5, bid_size_1..5,
  ask_price_1..5, ask_size_1..5, mid_price, spread

URL pattern: https://archive.pmxt.dev/{YYYY}/{MM}/{DD}/{HH}.parquet

CLI usage (inside bot container):
    python data/fetchers/pmxt_fetcher.py --start 2024-01-01 --end 2024-06-01
    python data/fetchers/pmxt_fetcher.py --start 2024-01-01 --end 2024-01-07 --save
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd
from loguru import logger

from config.schemas import OrderLevel, OrderbookSnapshot


class PmxtDownloader:
    """Downloads and loads historical orderbook snapshots from the pmxt archive."""

    BASE_URL = "https://archive.pmxt.dev"
    _DEFAULT_OUTPUT_DIR = "data/raw/pmxt"

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._client = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=120.0,
            follow_redirects=True,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def download_range(
        self,
        start: str,
        end: str,
        output_dir: str = _DEFAULT_OUTPUT_DIR,
    ) -> list[Path]:
        """Download all hourly Parquet files for the given date range.

        Args:
            start:      Start date inclusive, ISO format 'YYYY-MM-DD'.
            end:        End date inclusive, ISO format 'YYYY-MM-DD'.
            output_dir: Host directory to save .parquet files.

        Returns:
            List of paths to successfully downloaded files.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)

        downloaded: list[Path] = []
        current = start_dt

        while current < end_dt:
            path = self._hourly_path(current)
            dest = out / path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.exists():
                logger.debug("Already downloaded: {}", dest)
                downloaded.append(dest)
                current += timedelta(hours=1)
                continue

            try:
                logger.debug("Downloading {}", path)
                response = self._client.get(path)
                if response.status_code == 404:
                    logger.debug("Not found (404): {} — skipping", path)
                    current += timedelta(hours=1)
                    continue
                response.raise_for_status()
                dest.write_bytes(response.content)
                downloaded.append(dest)
                logger.debug("Saved {} ({:.1f} KB)", dest, len(response.content) / 1024)
            except httpx.HTTPStatusError as exc:
                logger.warning("HTTP {} for {}: skipping", exc.response.status_code, path)
            except httpx.RequestError as exc:
                logger.error("Request error for {}: {}", path, exc)

            current += timedelta(hours=1)

        logger.info(
            "pmxt download complete: {} files from {} to {}",
            len(downloaded), start, end,
        )
        return downloaded

    def load_parquet(self, parquet_path: Path) -> list[OrderbookSnapshot]:
        """Load a single Parquet file and return a list of OrderbookSnapshots.

        Args:
            parquet_path: Path to a .parquet file downloaded from pmxt.

        Returns:
            List of OrderbookSnapshot, one per row in the file.
        """
        df = pd.read_parquet(parquet_path)
        snapshots: list[OrderbookSnapshot] = []

        for _, row in df.iterrows():
            snapshot = _row_to_snapshot(row)
            if snapshot is not None:
                snapshots.append(snapshot)

        return snapshots

    def load_to_db(self, parquet_dir: str = _DEFAULT_OUTPUT_DIR) -> None:
        """Load all Parquet files in parquet_dir into PostgreSQL.

        Args:
            parquet_dir: Directory containing .parquet files.
        """
        files = sorted(Path(parquet_dir).rglob("*.parquet"))
        if not files:
            logger.warning("No Parquet files found in {}", parquet_dir)
            return

        logger.info("Loading {} Parquet files into DB", len(files))
        asyncio.run(self._load_files_async(files))

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _hourly_path(dt: datetime) -> str:
        """Build the pmxt archive path for a given UTC hour."""
        return f"/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{dt.hour:02d}.parquet"

    async def _load_files_async(self, files: list[Path]) -> None:
        from data.db import AsyncSessionFactory
        from data.repository import upsert_orderbook_snapshot

        total = 0
        async with AsyncSessionFactory() as session:
            for file in files:
                try:
                    snapshots = self.load_parquet(file)
                    for snap in snapshots:
                        await upsert_orderbook_snapshot(session, snap)
                    total += len(snapshots)
                    logger.debug("Loaded {} snapshots from {}", len(snapshots), file.name)
                except Exception as exc:
                    logger.error("Failed to load {}: {}", file, exc)

        logger.info("DB load complete: {} orderbook snapshots", total)


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _row_to_snapshot(row: pd.Series) -> OrderbookSnapshot | None:
    """Map a pmxt DataFrame row to an OrderbookSnapshot. Pure function."""
    try:
        token_id = str(row.get("token_id", row.get("tokenId", "")))
        ts_raw = row.get("timestamp", row.get("ts"))
        if ts_raw is None:
            return None

        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        else:
            ts = pd.Timestamp(ts_raw).to_pydatetime().replace(tzinfo=timezone.utc)

        bids = _extract_levels(row, side="bid", max_levels=5)
        asks = _extract_levels(row, side="ask", max_levels=5)

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else _safe_float(row.get("mid_price"))
        spread = (best_ask - best_bid) if best_bid and best_ask else _safe_float(row.get("spread"))

        return OrderbookSnapshot(
            token_id=token_id,
            timestamp=ts,
            bids=bids,
            asks=asks,
            mid_price=mid,
            spread=spread,
        )
    except Exception as exc:
        logger.warning("Skipping malformed row: {}", exc)
        return None


def _extract_levels(row: pd.Series, side: str, max_levels: int) -> list[OrderLevel]:
    """Extract bid or ask price/size levels from a DataFrame row. Pure function."""
    levels: list[OrderLevel] = []
    for i in range(1, max_levels + 1):
        price_key = f"{side}_price_{i}"
        size_key = f"{side}_size_{i}"
        price = _safe_float(row.get(price_key))
        size = _safe_float(row.get(size_key))
        if price is not None and size is not None and price > 0 and size > 0:
            try:
                levels.append(OrderLevel(price=price, size=size))
            except Exception:
                pass
    return levels


def _safe_float(value: object) -> float | None:
    """Safely coerce value to float. Pure function."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download pmxt historical orderbook snapshots")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--output-dir", default=PmxtDownloader._DEFAULT_OUTPUT_DIR)
    parser.add_argument("--save", action="store_true", help="Load downloaded files into DB")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    downloader = PmxtDownloader()
    files = downloader.download_range(args.start, args.end, output_dir=args.output_dir)
    logger.info("Downloaded {} files", len(files))
    if args.save and files:
        downloader.load_to_db(parquet_dir=args.output_dir)
