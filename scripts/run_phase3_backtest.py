"""
Phase 3 strategy comparison backtest.

Loads the dataset produced by scripts/prepare_backtest_data.py and runs all
three Phase 3 strategies on the identical market/price data, then prints a
side-by-side comparison table and saves individual JSON + chart reports.

Usage (inside the bot container):
    python scripts/run_phase3_backtest.py
    python scripts/run_phase3_backtest.py --dataset data/processed/backtest_dataset.json
    python scripts/run_phase3_backtest.py --capital 10000 --output results/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestEngine, BacktestResults
from backtest.fill_model import FillModel
from backtest.report import save_report
from config.schemas import BacktestMetrics, Market, PricePoint
from strategies.calibration_betting import CalibrationBetting
from strategies.market_maker import MarketMaker
from strategies.sum_to_one_arb import SumToOneArb


# ── CLI args ───────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Phase 3 strategy comparison backtest")
    p.add_argument("--dataset", default="data/processed/backtest_dataset.json")
    p.add_argument("--capital", type=float, default=10_000.0, help="Initial capital in USDC")
    p.add_argument("--output",  default="results/", help="Directory for report artefacts")
    p.add_argument("--seed",    type=int,   default=42,   help="Random seed for reproducibility")
    p.add_argument("--slippage", type=int,  default=10,   help="Slippage in bps (default 10)")
    return p.parse_args()


# ── Dataset loader ─────────────────────────────────────────────────────────────

def load_dataset(path: str) -> tuple[dict[str, Market], dict[str, list[PricePoint]]]:
    """Load market metadata and price series from a JSON dataset file."""
    data = json.loads(Path(path).read_text())

    market_data: dict[str, Market] = {}
    for m in data["markets"]:
        end_date = datetime.fromisoformat(m["end_date"]) if m.get("end_date") else None
        market_data[m["condition_id"]] = Market(
            condition_id=m["condition_id"],
            question=m.get("question", ""),
            category=m.get("category"),
            end_date=end_date,
            resolved=m.get("resolved", False),
            outcome=m.get("outcome"),
            volume_usd=m.get("volume_usd"),
            liquidity_usd=m.get("liquidity_usd"),
            yes_token_id=m.get("yes_token_id"),
            no_token_id=m.get("no_token_id"),
        )

    price_data: dict[str, list[PricePoint]] = {}
    for token_id, points in data["prices"].items():
        price_data[token_id] = [
            PricePoint(
                token_id=token_id,
                timestamp=datetime.fromisoformat(p["timestamp"]),
                price=p["price"],
            )
            for p in points
        ]

    logger.info(
        "Dataset loaded: {} markets, {} token series, {} total price points",
        len(market_data),
        len(price_data),
        sum(len(v) for v in price_data.values()),
    )
    return market_data, price_data


# ── Strategy factory ───────────────────────────────────────────────────────────

def build_strategies(market_data: dict[str, Market]) -> list:
    return [
        SumToOneArb(
            market_data=market_data,
            min_edge=0.02,
            max_position_usdc=500.0,
        ),
        MarketMaker(
            market_data=market_data,
            base_spread=0.04,
            window=5,
            order_size_usdc=200.0,
            max_inventory_pct=0.40,
        ),
        CalibrationBetting(
            market_data=market_data,
            min_edge=0.05,
            kelly_fraction=0.25,
            max_position_usdc=300.0,
            max_days_to_resolution=90,
        ),
    ]


# ── Runner ────────────────────────────────────────────────────────────────────

def run_strategy(
    strategy,
    price_data: dict[str, list[PricePoint]],
    market_data: dict[str, Market],
    initial_capital: float,
    fill_model: FillModel,
    random_seed: int,
) -> BacktestResults:
    engine = BacktestEngine(
        strategy=strategy,
        price_data=price_data,
        market_data=market_data,
        initial_capital=initial_capital,
        fill_model=fill_model,
        random_seed=random_seed,
    )
    return engine.run()


# ── Comparison table ───────────────────────────────────────────────────────────

def print_comparison(all_results: list[tuple[str, BacktestResults]]) -> None:
    sep = "─" * 90
    headers = [
        ("Strategy",           "<20"),
        ("Return %",           ">9"),
        ("Sharpe",             ">8"),
        ("Sortino",            ">8"),
        ("MaxDD %",            ">8"),
        ("Win Rate",           ">9"),
        ("Trades",             ">7"),
        ("Final $",            ">10"),
    ]
    header_line = "  ".join(f"{h:{fmt}}" for h, fmt in headers)

    print(f"\n{'Phase 3 Strategy Comparison':^90}")
    print(sep)
    print(header_line)
    print(sep)

    for name, res in all_results:
        m = res.metrics
        row = [
            (name,                                       "<20"),
            (f"{m.total_return_pct * 100:+.2f}",         ">9"),
            (f"{m.sharpe_ratio:.3f}",                    ">8"),
            (f"{m.sortino_ratio:.3f}",                   ">8"),
            (f"{m.max_drawdown_pct * 100:.2f}",          ">8"),
            (f"{m.win_rate * 100:.1f}%",                 ">9"),
            (f"{m.total_trades}",                        ">7"),
            (f"{m.final_capital:,.2f}",                  ">10"),
        ]
        print("  ".join(f"{v:{fmt}}" for v, fmt in row))

    print(sep)

    # Highlight best per metric
    if all_results:
        returns  = [(name, r.metrics.total_return_pct)    for name, r in all_results]
        sharpes  = [(name, r.metrics.sharpe_ratio)        for name, r in all_results]
        drawdowns= [(name, r.metrics.max_drawdown_pct)    for name, r in all_results]

        best_ret = max(returns,   key=lambda x: x[1])
        best_sh  = max(sharpes,   key=lambda x: x[1])
        best_dd  = min(drawdowns, key=lambda x: x[1])

        print(f"\n  Best return:   {best_ret[0]} ({best_ret[1]*100:+.2f}%)")
        print(f"  Best Sharpe:   {best_sh[0]} ({best_sh[1]:.3f})")
        print(f"  Min Drawdown:  {best_dd[0]} ({best_dd[1]*100:.2f}%)")
        print()

    # Phase 3 acceptance check
    print("Acceptance criteria check:")
    any_sharpe_ok  = any(r.metrics.sharpe_ratio > 1.0 for _, r in all_results)
    any_dd_ok      = all(r.metrics.max_drawdown_pct < 0.40 for _, r in all_results)
    print(f"  [ {'OK' if any_sharpe_ok else 'FAIL'} ] At least 1 strategy Sharpe > 1.0")
    print(f"  [ {'OK' if any_dd_ok    else 'FAIL'} ] No strategy Max Drawdown > 40%")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        logger.error("Dataset not found: {}\nRun scripts/prepare_backtest_data.py first.", dataset_path)
        sys.exit(1)

    market_data, price_data = load_dataset(args.dataset)
    fill_model = FillModel(slippage_bps=args.slippage)
    strategies = build_strategies(market_data)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[tuple[str, BacktestResults]] = []

    for strategy in strategies:
        logger.info("Running backtest: {}", strategy.name)
        results = run_strategy(
            strategy=strategy,
            price_data=price_data,
            market_data=market_data,
            initial_capital=args.capital,
            fill_model=fill_model,
            random_seed=args.seed,
        )
        all_results.append((strategy.name, results))
        save_report(results, output_dir=output_dir)
        logger.info(
            "{}: return={:+.2f}%  sharpe={:.3f}  drawdown={:.2f}%  trades={}",
            strategy.name,
            results.metrics.total_return_pct * 100,
            results.metrics.sharpe_ratio,
            results.metrics.max_drawdown_pct * 100,
            results.metrics.total_trades,
        )

    print_comparison(all_results)


if __name__ == "__main__":
    main()
