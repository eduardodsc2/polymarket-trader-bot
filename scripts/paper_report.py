"""
Paper trading performance report.

Reads all paper-mode trades from the DB and computes:
  - Total fills, buy/sell breakdown
  - Round-trip PnL per condition_id (BUY cost vs SELL revenue)
  - Win rate (% of round-trips where PnL > 0)
  - Avg slippage (inferred from fill price vs market price)
  - Equity curve: portfolio value over time from snapshots
  - Per-strategy summary

Usage (inside Docker):
    docker compose run --rm bot python scripts/paper_report.py
    docker compose run --rm bot python scripts/paper_report.py --strategy market_maker
    docker compose run --rm bot python scripts/paper_report.py --since 2025-01-01
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

from config.settings import Settings


def main(strategy_filter: str | None = None, since: str | None = None) -> None:
    settings = Settings()

    # Use sync URL (psycopg2 — simpler for one-shot scripts)
    conn = psycopg2.connect(settings.database_url_sync.replace("postgresql+psycopg2://", "postgresql://"))
    cur = conn.cursor()

    # ── 1. Fetch trades ───────────────────────────────────────────────────────
    where_clauses = ["mode = 'paper'"]
    params: list = []

    if strategy_filter:
        where_clauses.append("strategy = %s")
        params.append(strategy_filter)

    if since:
        where_clauses.append("executed_at >= %s")
        params.append(since)

    where_sql = " AND ".join(where_clauses)

    cur.execute(
        f"""
        SELECT strategy, condition_id, token_id, side, size_usd, price, fee_usd, executed_at
        FROM trades
        WHERE {where_sql}
        ORDER BY executed_at ASC
        """,
        params,
    )
    rows = cur.fetchall()

    if not rows:
        print("No paper trades found. Run the bot in paper mode first.")
        return

    print(f"\n{'='*60}")
    print(f"  PAPER TRADING REPORT")
    if strategy_filter:
        print(f"  Strategy: {strategy_filter}")
    if since:
        print(f"  Since: {since}")
    print(f"{'='*60}\n")

    # ── 2. Basic stats ────────────────────────────────────────────────────────
    strategies = defaultdict(list)
    for row in rows:
        strategies[row[0]].append(row)

    total_buys = sum(1 for r in rows if r[3] == "BUY")
    total_sells = sum(1 for r in rows if r[3] == "SELL")
    total_volume = sum(r[4] for r in rows)

    print(f"Total fills      : {len(rows)}")
    print(f"  BUY            : {total_buys}")
    print(f"  SELL           : {total_sells}")
    print(f"Total volume USD : ${total_volume:,.2f}")
    print(f"Strategies       : {', '.join(strategies.keys())}")
    print()

    # ── 3. Per-strategy round-trip PnL ────────────────────────────────────────
    for strat, strat_rows in strategies.items():
        print(f"── Strategy: {strat} ──────────────────────────────────")

        # Match BUY/SELL by token_id (same asset) using FIFO.
        # Row layout: (strategy[0], condition_id[1], token_id[2], side[3],
        #              size_usd[4], price[5], fee_usd[6], executed_at[7])
        by_token: dict[str, list] = defaultdict(list)
        for r in strat_rows:
            by_token[r[2]].append(r)  # r[2] = token_id

        closed_trades: list[float] = []   # PnL per round-trip
        open_buys: list = []              # BUYs without matching SELL
        orphan_sells: int = 0            # SELLs with no prior BUY (strategy bug indicator)

        for token_id, token_trades in by_token.items():
            buys = [t for t in token_trades if t[3] == "BUY"]
            sells = [t for t in token_trades if t[3] == "SELL"]

            # FIFO match: pair each SELL with the earliest unpaired BUY
            buy_queue = list(buys)
            for sell in sells:
                sell_price = float(sell[5])
                sell_size = float(sell[4])
                tokens_sold = sell_size / sell_price if sell_price > 0 else 0

                if buy_queue:
                    buy = buy_queue.pop(0)
                    buy_price = float(buy[5])
                    # PnL = tokens bought back at buy_price, valued at sell_price
                    # (we hold tokens, sell at sell_price, cost was buy_price per token)
                    pnl = tokens_sold * (sell_price - buy_price)
                    closed_trades.append(pnl)
                else:
                    orphan_sells += 1  # SELL with no prior BUY — cross-token mismatch

            open_buys.extend(buy_queue)  # unmatched BUYs

        strat_buys = sum(1 for r in strat_rows if r[3] == "BUY")
        strat_sells = sum(1 for r in strat_rows if r[3] == "SELL")
        strat_volume = sum(r[4] for r in strat_rows)

        print(f"  Fills: {len(strat_rows)} ({strat_buys} BUY, {strat_sells} SELL)")
        print(f"  Volume: ${strat_volume:,.2f}")
        print(f"  Open positions: {len(open_buys)} unmatched BUYs")
        if orphan_sells > 0:
            print(f"  ⚠  Orphan SELLs: {orphan_sells} (SELLs with no matching BUY on same token_id)")
            print(f"     → Indicates strategy is crossing YES/NO token pairs for same condition")

        if closed_trades:
            wins = [p for p in closed_trades if p > 0]
            losses = [p for p in closed_trades if p <= 0]
            win_rate = len(wins) / len(closed_trades) * 100
            total_pnl = sum(closed_trades)
            avg_pnl = total_pnl / len(closed_trades)
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0

            print(f"  Round-trips: {len(closed_trades)}")
            print(f"  Win rate   : {win_rate:.1f}%")
            print(f"  Total PnL  : ${total_pnl:+.2f}")
            print(f"  Avg PnL/trade: ${avg_pnl:+.4f}")
            print(f"  Avg win    : ${avg_win:+.4f}")
            print(f"  Avg loss   : ${avg_loss:+.4f}")

            if wins and losses:
                profit_factor = abs(sum(wins)) / abs(sum(losses))
                print(f"  Profit factor: {profit_factor:.2f}x")
        else:
            print(f"  No closed round-trips yet (all positions open)")

        print()

    # ── 4. Portfolio equity curve (from snapshots) ────────────────────────────
    cur.execute(
        """
        SELECT total_value_usd, realized_pnl, snapshot_at
        FROM portfolio_snapshots
        WHERE mode = 'paper'
        ORDER BY snapshot_at ASC
        """,
    )
    snapshots = cur.fetchall()

    if snapshots:
        initial = snapshots[0][0]
        final = snapshots[-1][0]
        peak = max(s[0] for s in snapshots)
        trough_after_peak = min(
            s[0] for i, s in enumerate(snapshots)
            if s[0] == peak or (i > 0 and any(sx[0] == peak for sx in snapshots[:i]))
        )
        max_dd = (peak - trough_after_peak) / peak * 100 if peak > 0 else 0.0
        total_return = (final - initial) / initial * 100 if initial > 0 else 0.0

        print(f"── Equity Curve ({len(snapshots)} snapshots) ────────────────────")
        print(f"  Initial value : ${initial:,.2f}")
        print(f"  Final value   : ${final:,.2f}")
        print(f"  Total return  : {total_return:+.2f}%")
        print(f"  Peak value    : ${peak:,.2f}")
        print(f"  Max drawdown  : {max_dd:.2f}%")
        print(f"  First snapshot: {snapshots[0][2]}")
        print(f"  Last snapshot : {snapshots[-1][2]}")
        print()
    else:
        print("── Equity Curve: no snapshots yet (snapshots saved every 60s) ──\n")

    # ── 5. Time range ─────────────────────────────────────────────────────────
    if rows:
        first_ts = rows[0][7]
        last_ts = rows[-1][7]
        duration = last_ts - first_ts if isinstance(last_ts, datetime) else None
        print(f"── Session ───────────────────────────────────────────────")
        print(f"  First trade: {first_ts}")
        print(f"  Last trade : {last_ts}")
        if duration:
            print(f"  Duration   : {duration}")
        print()

    cur.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper trading performance report")
    parser.add_argument("--strategy", default=None, help="Filter by strategy name")
    parser.add_argument("--since", default=None, help="Filter trades since date (YYYY-MM-DD)")
    args = parser.parse_args()
    main(strategy_filter=args.strategy, since=args.since)
