"""Phase 1 acceptance criteria runner.

Run inside the bot container:
    python tests/acceptance_phase1.py
"""
import httpx
import json
import sys
import time

from data.fetchers.clob_fetcher import CLOBFetcher
from data.fetchers.gamma_fetcher import GammaFetcher
from data.validate import build_quality_report

PASS = "\033[32mPASSED\033[0m"
FAIL = "\033[31mFAILED\033[0m"
errors = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}  {detail}")
        errors.append(name)


print("=" * 60)
print("Phase 1 — Acceptance Criteria")
print("=" * 60)

# ── Criterion 1: 500+ resolved markets ────────────────────────
print("\n[1] Resolved markets 2024 (min 500)")
gf = GammaFetcher()
markets = gf.get_resolved_markets("2024-01-01", "2024-12-31")
check("500+ resolved markets", len(markets) >= 500, f"got {len(markets)}")
check("Markets have Pydantic type", all(hasattr(m, "model_fields") for m in markets[:5]))
check("Markets have condition_id", all(m.condition_id for m in markets[:5]))
check("Markets have yes_token_id", any(m.yes_token_id for m in markets[:20]))
print(f"  → {len(markets):,} markets fetched")

# ── Criterion 2: Price series reconstruction ──────────────────
print("\n[2] Price history for active market (7d, fidelity=60min)")
r = httpx.get("https://gamma-api.polymarket.com/markets", params={
    "limit": 10, "closed": "false", "volume_num_min": 100000
})
active = r.json()
m = active[0]
ids = json.loads(m.get("clobTokenIds", "[]"))
token = ids[0]
cf = CLOBFetcher()
end_ts = int(time.time())
start_ts = end_ts - 7 * 24 * 3600

prices = cf.get_price_history(token, start_ts, end_ts, fidelity=60)
check("Price points returned", len(prices) > 0, f"got {len(prices)}")
check("PricePoint Pydantic type", all(hasattr(p, "model_fields") for p in prices[:5]))
check("Prices in [0,1]", all(0.0 <= p.price <= 1.0 for p in prices))
check("Timestamps ordered", prices == sorted(prices, key=lambda x: x.timestamp))
print(f"  → {len(prices)} price points for: {m['question'][:55]}")
if prices:
    print(f"  → Range: {prices[0].timestamp.date()} → {prices[-1].timestamp.date()}")

# ── Criterion 3: Data validation <1% anomaly rate ─────────────
print("\n[3] Data validation <1% anomaly rate")
report = build_quality_report(prices)
check("Quality report generated", report is not None)
check("Anomaly rate <1%", report.anomaly_rate_pct < 1.0, f"{report.anomaly_rate_pct:.4f}%")
check("QualityReport has checks_passed", len(report.checks_passed) > 0)
print(f"  → {report.anomaly_count}/{report.total_price_points} anomalies ({report.anomaly_rate_pct:.4f}%)")
print(f"  → Passed: {report.checks_passed}")

# ── Summary ────────────────────────────────────────────────────
print("\n" + "=" * 60)
if not errors:
    print(f"\033[32mALL CRITERIA PASSED\033[0m")
    sys.exit(0)
else:
    print(f"\033[31mFAILED: {errors}\033[0m")
    sys.exit(1)
