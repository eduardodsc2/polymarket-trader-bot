"""
Backtest report generation.

Produces:
  1. JSON summary file  (results/backtest_{strategy}_{date}.json)
  2. Equity curve plot  (results/equity_{strategy}_{date}.png)
  3. Drawdown chart     (results/drawdown_{strategy}_{date}.png)

All functions are side-effect-only (I/O). Computation stays in metrics.py.

Usage:
    from backtest.report import save_report
    save_report(results, output_dir="results")
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from backtest.engine import BacktestResults
from config.schemas import BacktestMetrics, PortfolioSnapshot


def save_report(
    results: BacktestResults,
    output_dir: str | Path = "results",
) -> Path:
    """
    Persist all report artefacts for a completed backtest.

    Args:
        results:    BacktestResults from engine.run().
        output_dir: Directory to write files (created if it doesn't exist).

    Returns:
        Path to the JSON summary file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    tag = _run_tag(results.metrics)

    json_path = _write_json(results.metrics, out, tag)
    _write_equity_plot(results.snapshots, results.metrics.strategy, out, tag)
    _write_drawdown_plot(results.snapshots, results.metrics.strategy, out, tag)

    logger.info("Report saved to {}", out)
    return json_path


# ── JSON summary ───────────────────────────────────────────────────────────────

def _write_json(metrics: BacktestMetrics, out: Path, tag: str) -> Path:
    path = out / f"backtest_{tag}.json"
    data = metrics.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("JSON summary → {}", path)
    return path


# ── Equity curve plot ──────────────────────────────────────────────────────────

def _write_equity_plot(
    snapshots: list[PortfolioSnapshot],
    strategy_name: str,
    out: Path,
    tag: str,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")   # non-interactive backend — safe inside Docker
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib not installed — skipping equity plot")
        return

    if not snapshots:
        return

    timestamps = [s.timestamp for s in snapshots]
    values = [s.total_value_usd for s in snapshots]
    initial = values[0] if values else 1.0

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(timestamps, values, linewidth=1.5, color="#2196F3")
    ax.axhline(initial, color="#9E9E9E", linestyle="--", linewidth=0.8)
    ax.set_title(f"Equity Curve — {strategy_name}", fontsize=13)
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio Value (USDC)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)

    path = out / f"equity_{tag}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Equity curve → {}", path)


# ── Drawdown chart ─────────────────────────────────────────────────────────────

def _write_drawdown_plot(
    snapshots: list[PortfolioSnapshot],
    strategy_name: str,
    out: Path,
    tag: str,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
    except ImportError:
        logger.warning("matplotlib not installed — skipping drawdown plot")
        return

    if not snapshots:
        return

    timestamps = [s.timestamp for s in snapshots]
    values = np.array([s.total_value_usd for s in snapshots], dtype=float)
    peaks = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(peaks > 0, (peaks - values) / peaks * 100, 0.0)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.fill_between(timestamps, -drawdowns, 0, color="#F44336", alpha=0.6)
    ax.plot(timestamps, -drawdowns, linewidth=0.8, color="#B71C1C")
    ax.set_title(f"Drawdown — {strategy_name}", fontsize=13)
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)

    path = out / f"drawdown_{tag}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Drawdown chart → {}", path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_tag(metrics: BacktestMetrics) -> str:
    """Generate a filename-safe tag: {strategy}_{YYYYMMDD}."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe_name = metrics.strategy.replace(" ", "_").lower()
    return f"{safe_name}_{date_str}"
