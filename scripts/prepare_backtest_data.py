"""
Prepare a clean backtest dataset for Phase 3 strategy evaluation.

What this script does:
  1. Fetches the top N resolved markets from 2024 (filtered by volume).
  2. For each market, fetches daily YES and NO token price histories from CLOB.
  3. Skips markets with fewer than min_price_points data points.
  4. Saves the result to data/processed/backtest_dataset.json.

The output JSON is directly loadable by scripts/run_phase3_backtest.py.

Usage (inside the bot container):
    python scripts/prepare_backtest_data.py
    python scripts/prepare_backtest_data.py --markets 200 --min-volume 50000
    python scripts/prepare_backtest_data.py --start 2024-01-01 --end 2024-06-30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# Add repo root to path so local imports resolve inside the container
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.fetchers.clob_fetcher import CLOBFetcher
from data.fetchers.gamma_fetcher import GammaFetcher
from config.schemas import Market, PricePoint


# ── CLI args ───────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Phase 3 backtest dataset")
    p.add_argument("--start",      default="2024-01-01", help="Market end_date range start")
    p.add_argument("--end",        default="2024-12-31", help="Market end_date range end")
    p.add_argument("--markets",    type=int,   default=100,    help="Target number of markets")
    p.add_argument("--min-volume", type=float, default=10_000, help="Min USDC volume per market")
    p.add_argument("--min-points", type=int,   default=10,     help="Min price points to include a market")
    p.add_argument("--fidelity",   type=int,   default=1440,   help="Candle resolution in minutes (1440=daily)")
    p.add_argument("--max-raw",    type=int,   default=5_000,  help="Max raw records from Gamma pagination")
    p.add_argument("--output",     default="data/processed/backtest_dataset.json")
    p.add_argument("--delay",      type=float, default=0.1,    help="Seconds between CLOB requests")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Fetch market metadata ───────────────────────────────────────────────

    logger.info("Step 1: fetching resolved markets from Gamma API...")
    gamma = GammaFetcher()
    raw_markets = gamma.get_resolved_markets(
        start_date=args.start,
        end_date=args.end,
        min_volume=args.min_volume,
        max_markets=args.max_raw,
    )

    # Sort by volume descending — take the most liquid markets first
    raw_markets.sort(key=lambda m: m.volume_usd or 0.0, reverse=True)
    candidate_markets = [
        m for m in raw_markets
        if m.yes_token_id and m.no_token_id and m.condition_id
    ][:args.markets]

    logger.info(
        "Candidate markets after sort+filter: {} (target: {})",
        len(candidate_markets), args.markets,
    )

    if not candidate_markets:
        logger.error("No candidate markets found — check date range and min-volume.")
        sys.exit(1)

    # ── 2. Fetch price histories ───────────────────────────────────────────────

    start_ts = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp())

    clob = CLOBFetcher()
    dataset_markets: list[dict] = []
    dataset_prices: dict[str, list[dict]] = {}   # token_id → list of {t, p}

    included = 0
    skipped = 0

    for idx, market in enumerate(candidate_markets):
        logger.info(
            "[{}/{}] {} — {}",
            idx + 1, len(candidate_markets),
            market.condition_id[:12],
            (market.question or "")[:60],
        )

        yes_prices = _fetch_prices(clob, market.yes_token_id, start_ts, end_ts, args.fidelity)
        if args.delay:
            time.sleep(args.delay)
        no_prices  = _fetch_prices(clob, market.no_token_id,  start_ts, end_ts, args.fidelity)
        if args.delay:
            time.sleep(args.delay)

        if len(yes_prices) < args.min_points:
            logger.warning(
                "Skipping {} — only {} YES price points (min={})",
                market.condition_id[:12], len(yes_prices), args.min_points,
            )
            skipped += 1
            continue

        dataset_markets.append(_market_to_dict(market))
        dataset_prices[market.yes_token_id] = [_price_to_dict(p) for p in yes_prices]
        dataset_prices[market.no_token_id]  = [_price_to_dict(p) for p in no_prices]
        included += 1

    logger.info("Markets included: {}  skipped: {}", included, skipped)

    # ── 3. Save ────────────────────────────────────────────────────────────────

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_date":   args.start,
        "end_date":     args.end,
        "markets":      dataset_markets,
        "prices":       dataset_prices,
    }
    output_path.write_text(json.dumps(payload, indent=2))
    logger.info(
        "Dataset saved → {} ({} markets, {} token series, {:.1f} KB)",
        output_path,
        len(dataset_markets),
        len(dataset_prices),
        output_path.stat().st_size / 1024,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch_prices(
    clob: CLOBFetcher,
    token_id: str | None,
    start_ts: int,
    end_ts: int,
    fidelity: int,
) -> list[PricePoint]:
    if not token_id:
        return []
    try:
        return clob.get_price_history(token_id, start_ts, end_ts, fidelity)
    except Exception as exc:
        logger.warning("CLOB price fetch failed for {}: {}", token_id[:16], exc)
        return []


def _market_to_dict(m: Market) -> dict:
    return {
        "condition_id":  m.condition_id,
        "question":      m.question,
        "category":      m.category,
        "end_date":      m.end_date.isoformat() if m.end_date else None,
        "resolved":      m.resolved,
        "outcome":       m.outcome,
        "volume_usd":    m.volume_usd,
        "liquidity_usd": m.liquidity_usd,
        "yes_token_id":  m.yes_token_id,
        "no_token_id":   m.no_token_id,
    }


def _price_to_dict(p: PricePoint) -> dict:
    return {"timestamp": p.timestamp.isoformat(), "price": p.price}


if __name__ == "__main__":
    main()
