"""
Phase 4 strategy comparison backtest.

Adds ValueBetting (LLM-assisted) to the Phase 3 baseline comparison.

In this script a MockLLMEstimator replaces the real Claude API so the backtest
runs without any API cost or network access. The mock adds zero-mean Gaussian
noise to the market price — this simulates a completely uninformative model so
ValueBetting behaves close to random.

The point of this script is to verify the full ValueBetting infrastructure
works end-to-end (market filtering, news context pipeline, DecisionEngine,
Kelly sizing, backtest integration). Real LLM performance will be measured
during paper trading with actual Claude API calls.

Usage (inside the bot container):
    python scripts/run_phase4_backtest.py
    python scripts/run_phase4_backtest.py --dataset data/processed/backtest_dataset.json
    python scripts/run_phase4_backtest.py --capital 10000 --output results/
    python scripts/run_phase4_backtest.py --llm-noise 0.0   # zero-noise oracle baseline
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestEngine, BacktestResults
from backtest.fill_model import FillModel
from backtest.report import save_report
from config.schemas import BacktestMetrics, LLMEstimate, Market, NewsArticle, PricePoint
from strategies.calibration_betting import CalibrationBetting
from strategies.value_betting import ValueBetting


# ── Mock LLM Estimator ─────────────────────────────────────────────────────────


class MockLLMEstimator:
    """
    Deterministic fake LLM estimator for backtest infrastructure testing.

    Adds Gaussian noise (std=noise_std) to the market price to simulate an
    uninformative model. The same seed always produces identical results.

    With noise_std=0.0 the estimate equals the market price → zero edge on every
    market → no trades (useful as a sanity-check baseline).

    With noise_std>0 random noise is added — over many markets the expected
    value is zero, demonstrating that ValueBetting with no real information
    performs no better than random.
    """

    def __init__(self, noise_std: float = 0.10, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._noise_std = noise_std

    def estimate(
        self,
        condition_id: str,
        question: str,
        category: str,
        resolution_date: str,
        current_price: float,
        articles: list[NewsArticle],
    ) -> LLMEstimate:
        noise = self._rng.gauss(0.0, self._noise_std)
        probability = max(0.05, min(0.95, current_price + noise))
        return LLMEstimate(
            condition_id=condition_id,
            model="mock-gaussian",
            prompt_hash=f"mock-{condition_id[:8]}",
            probability=round(probability, 4),
            confidence=0.66,  # MEDIUM — below HIGH to be conservative
            reasoning=f"Mock estimate: market={current_price:.2f} noise={noise:+.3f}",
        )


# ── Dataset loader (copied from run_phase3_backtest.py) ────────────────────────


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


def build_strategies(
    market_data: dict[str, Market],
    llm_noise_std: float,
    seed: int,
) -> list:
    return [
        # Phase 3 baseline — for comparison
        CalibrationBetting(
            market_data=market_data,
            min_edge=0.05,
            kelly_fraction=0.25,
            max_position_usdc=300.0,
            max_days_to_resolution=90,
        ),
        # Phase 4 — ValueBetting with mock LLM (noise around market price)
        ValueBetting(
            market_data=market_data,
            llm_estimator=MockLLMEstimator(noise_std=llm_noise_std, seed=seed),
            news_fetcher=None,          # no news context in this offline backtest
            min_edge=0.05,
            kelly_fraction=0.25,
            max_position_usdc=300.0,
            min_volume_usd=0.0,         # include all markets in dataset
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
        ("Strategy",   "<25"),
        ("Return %",   ">9"),
        ("Sharpe",     ">8"),
        ("Sortino",    ">8"),
        ("MaxDD %",    ">8"),
        ("Win Rate",   ">9"),
        ("Trades",     ">7"),
        ("Final $",    ">10"),
    ]
    header_line = "  ".join(f"{h:{fmt}}" for h, fmt in headers)

    print(f"\n{'Phase 4 Strategy Comparison (with mock LLM)':^90}")
    print(sep)
    print(header_line)
    print(sep)

    for name, res in all_results:
        m = res.metrics
        row = [
            (name,                                         "<25"),
            (f"{m.total_return_pct * 100:+.2f}",           ">9"),
            (f"{m.sharpe_ratio:.3f}",                      ">8"),
            (f"{m.sortino_ratio:.3f}",                     ">8"),
            (f"{m.max_drawdown_pct * 100:.2f}",            ">8"),
            (f"{m.win_rate * 100:.1f}%",                   ">9"),
            (f"{m.total_trades}",                          ">7"),
            (f"{m.final_capital:,.2f}",                    ">10"),
        ]
        print("  ".join(f"{v:{fmt}}" for v, fmt in row))

    print(sep)

    if all_results:
        returns  = [(name, r.metrics.total_return_pct) for name, r in all_results]
        sharpes  = [(name, r.metrics.sharpe_ratio)     for name, r in all_results]
        drawdowns= [(name, r.metrics.max_drawdown_pct) for name, r in all_results]

        best_ret = max(returns,    key=lambda x: x[1])
        best_sh  = max(sharpes,    key=lambda x: x[1])
        best_dd  = min(drawdowns,  key=lambda x: x[1])

        print(f"\n  Best return:  {best_ret[0]} ({best_ret[1]*100:+.2f}%)")
        print(f"  Best Sharpe:  {best_sh[0]} ({best_sh[1]:.3f})")
        print(f"  Min Drawdown: {best_dd[0]} ({best_dd[1]*100:.2f}%)")

    print()
    print("NOTE: ValueBetting uses a MockLLMEstimator (Gaussian noise around market price).")
    print("      This mock has zero edge by design — it tests infrastructure, not LLM quality.")
    print("      Real LLM performance requires paper trading with Claude API calls.")
    print()

    # Phase 4 acceptance check
    print("Acceptance criteria check:")
    vb = next((r for n, r in all_results if "value_betting" in n), None)
    cb = next((r for n, r in all_results if "calibration" in n), None)

    infra_ok = vb is not None and vb.metrics.total_trades > 0
    no_crash  = all(r.metrics.max_drawdown_pct < 1.0 for _, r in all_results)

    print(f"  [ {'OK' if infra_ok else 'FAIL'} ] ValueBetting executed at least 1 trade")
    print(f"  [ {'OK' if no_crash  else 'FAIL'} ] No strategy crashed (drawdown < 100%)")
    if cb and vb:
        print(f"  [NOTE] CalibrationBetting Sharpe={cb.metrics.sharpe_ratio:.3f}, "
              f"ValueBetting(mock) Sharpe={vb.metrics.sharpe_ratio:.3f}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Phase 4 ValueBetting backtest comparison")
    p.add_argument("--dataset",   default="data/processed/backtest_dataset.json")
    p.add_argument("--capital",   type=float, default=10_000.0)
    p.add_argument("--output",    default="results/")
    p.add_argument("--seed",      type=int,   default=42)
    p.add_argument("--slippage",  type=int,   default=10)
    p.add_argument(
        "--llm-noise", type=float, default=0.10,
        help="Std dev of Gaussian noise added to market price by mock LLM (default: 0.10)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        logger.error(
            "Dataset not found: {}\nRun scripts/prepare_backtest_data.py first.",
            dataset_path,
        )
        sys.exit(1)

    market_data, price_data = load_dataset(args.dataset)
    fill_model = FillModel(slippage_bps=args.slippage)
    strategies = build_strategies(market_data, args.llm_noise, args.seed)

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
