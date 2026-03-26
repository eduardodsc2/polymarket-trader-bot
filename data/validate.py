"""Data validation and quality report generator.

All check_* functions are pure: same input → same output, no I/O.
The run_quality_report() function orchestrates checks and writes the result.

CLI usage (inside bot container):
    python data/validate.py --output data/quality_report.json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from loguru import logger
from pydantic import BaseModel

from config.schemas import PricePoint


# ── Report schema ──────────────────────────────────────────────────────────────

class AnomalyDetail(BaseModel):
    token_id: str
    timestamp: datetime | None = None
    issue: str


class QualityReport(BaseModel):
    generated_at: datetime
    total_price_points: int
    anomaly_count: int
    anomaly_rate_pct: float
    anomalies: list[AnomalyDetail]
    checks_passed: list[str]
    checks_failed: list[str]

    @property
    def is_clean(self) -> bool:
        return self.anomaly_rate_pct < 1.0


# ── Pure check functions ───────────────────────────────────────────────────────

def check_price_range(prices: Sequence[PricePoint]) -> list[AnomalyDetail]:
    """Flag price points where price < 0 or price > 1. Pure function."""
    return [
        AnomalyDetail(
            token_id=p.token_id,
            timestamp=p.timestamp,
            issue=f"price out of range: {p.price}",
        )
        for p in prices
        if not (0.0 <= p.price <= 1.0)
    ]


def check_timestamp_gaps(
    prices: Sequence[PricePoint],
    max_gap: timedelta = timedelta(hours=2),
) -> list[AnomalyDetail]:
    """Flag gaps larger than max_gap in the price series per token. Pure function."""
    by_token: dict[str, list[PricePoint]] = {}
    for p in prices:
        by_token.setdefault(p.token_id, []).append(p)

    anomalies: list[AnomalyDetail] = []
    for token_id, series in by_token.items():
        sorted_series = sorted(series, key=lambda x: x.timestamp)
        for i in range(1, len(sorted_series)):
            gap = sorted_series[i].timestamp - sorted_series[i - 1].timestamp
            if gap > max_gap:
                anomalies.append(AnomalyDetail(
                    token_id=token_id,
                    timestamp=sorted_series[i].timestamp,
                    issue=f"timestamp gap of {gap} (>{max_gap})",
                ))

    return anomalies


def check_duplicates(prices: Sequence[PricePoint]) -> list[AnomalyDetail]:
    """Flag duplicate (token_id, timestamp) pairs. Pure function."""
    seen: set[tuple[str, datetime]] = set()
    anomalies: list[AnomalyDetail] = []

    for p in prices:
        key = (p.token_id, p.timestamp)
        if key in seen:
            anomalies.append(AnomalyDetail(
                token_id=p.token_id,
                timestamp=p.timestamp,
                issue="duplicate (token_id, timestamp)",
            ))
        seen.add(key)

    return anomalies


def check_negative_volume(prices: Sequence[PricePoint]) -> list[AnomalyDetail]:
    """Flag price points with negative volume. Pure function."""
    return [
        AnomalyDetail(
            token_id=p.token_id,
            timestamp=p.timestamp,
            issue=f"negative volume: {p.volume}",
        )
        for p in prices
        if p.volume is not None and p.volume < 0
    ]


def check_yes_no_sum(
    yes_prices: Sequence[PricePoint],
    no_prices: Sequence[PricePoint],
    tolerance: float = 0.05,
) -> list[AnomalyDetail]:
    """Flag timestamps where YES + NO price deviates more than tolerance from 1.0.

    Args:
        yes_prices:  Price series for the YES token.
        no_prices:   Price series for the NO token.
        tolerance:   Maximum allowed deviation from 1.0 (default 5%).

    Returns:
        List of anomalies where |YES + NO - 1| > tolerance.
    """
    no_by_ts: dict[datetime, float] = {p.timestamp: p.price for p in no_prices}
    anomalies: list[AnomalyDetail] = []

    for p in yes_prices:
        no_price = no_by_ts.get(p.timestamp)
        if no_price is None:
            continue
        total = p.price + no_price
        if abs(total - 1.0) > tolerance:
            anomalies.append(AnomalyDetail(
                token_id=p.token_id,
                timestamp=p.timestamp,
                issue=f"YES+NO={total:.4f} (expected 1.0 ±{tolerance})",
            ))

    return anomalies


# ── Report orchestrator ────────────────────────────────────────────────────────

def build_quality_report(prices: Sequence[PricePoint]) -> QualityReport:
    """Run all checks on a list of PricePoints and return a QualityReport.

    This function is pure except for reading the current time.
    """
    all_anomalies: list[AnomalyDetail] = []
    passed: list[str] = []
    failed: list[str] = []

    def run_check(name: str, result: list[AnomalyDetail]) -> None:
        if result:
            failed.append(name)
            all_anomalies.extend(result)
        else:
            passed.append(name)

    run_check("price_range", check_price_range(prices))
    run_check("timestamp_gaps", check_timestamp_gaps(prices))
    run_check("duplicates", check_duplicates(prices))
    run_check("negative_volume", check_negative_volume(prices))

    total = len(prices)
    rate = (len(all_anomalies) / total * 100) if total > 0 else 0.0

    return QualityReport(
        generated_at=datetime.now(timezone.utc),
        total_price_points=total,
        anomaly_count=len(all_anomalies),
        anomaly_rate_pct=round(rate, 4),
        anomalies=all_anomalies,
        checks_passed=passed,
        checks_failed=failed,
    )


def run_quality_report(prices: Sequence[PricePoint], output_path: str = "data/quality_report.json") -> QualityReport:
    """Build and write the quality report to disk.

    Args:
        prices:      Sequence of PricePoints to validate.
        output_path: Path to write the JSON report.

    Returns:
        The QualityReport (also written to output_path).
    """
    report = build_quality_report(prices)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2))

    if report.is_clean:
        logger.info(
            "Quality check PASSED: {}/{} anomalies ({:.4f}%)",
            report.anomaly_count, report.total_price_points, report.anomaly_rate_pct,
        )
    else:
        logger.warning(
            "Quality check FAILED: {}/{} anomalies ({:.4f}%) — report: {}",
            report.anomaly_count, report.total_price_points, report.anomaly_rate_pct, output_path,
        )

    return report


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run data quality validation")
    parser.add_argument("--output", default="data/quality_report.json", help="Output JSON path")
    parser.add_argument(
        "--token-id", help="Validate only this token_id (from DB). Omit to validate all."
    )
    return parser.parse_args()


if __name__ == "__main__":
    import asyncio

    args = _parse_args()

    async def _load_and_validate() -> None:
        from sqlalchemy import select, text
        from data.db import AsyncSessionFactory

        async with AsyncSessionFactory() as session:
            query = "SELECT token_id, timestamp, price, volume FROM prices"
            if args.token_id:
                query += f" WHERE token_id = :tid"
                result = await session.execute(text(query), {"tid": args.token_id})
            else:
                result = await session.execute(text(query))
            rows = result.fetchall()

        prices = [
            PricePoint(
                token_id=row[0],
                timestamp=row[1],
                price=float(row[2]),
                volume=float(row[3]) if row[3] is not None else None,
            )
            for row in rows
        ]

        logger.info("Loaded {} price points for validation", len(prices))
        run_quality_report(prices, output_path=args.output)

    asyncio.run(_load_and_validate())
