"""
FastAPI dashboard backend — DB-backed (reads from PostgreSQL).

Endpoints:
  GET  /health              → Docker health check
  GET  /api/snapshot        → Latest portfolio snapshot from DB
  GET  /api/trades          → Recent trades from DB (last 50)
  GET  /api/metrics         → Win rate, PnL, trade counts from DB
  WS   /ws                  → Push stream: polls DB every 5s, pushes on change
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Polymarket Dashboard", version="0.6.0")

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── DB pool ───────────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None

_MODE = os.getenv("BOT_MODE", "paper")
_STRATEGY = os.getenv("PAPER_STRATEGY", os.getenv("ACTIVE_STRATEGY", "market_maker"))
_INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL_USD", "500"))


def _db_url() -> str:
    url = os.getenv(
        "DASHBOARD_DB_URL",
        "postgresql+asyncpg://polymarket:changeme@db:5432/polymarket_bot",
    )
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=3)
    return _pool


@app.on_event("startup")
async def startup() -> None:
    for _ in range(10):
        try:
            await _get_pool()
            return
        except Exception:
            await asyncio.sleep(2)


# ── Serialization helpers ─────────────────────────────────────────────────────

def _row(record: asyncpg.Record) -> dict:
    """Convert asyncpg Record to JSON-serializable dict."""
    out: dict = {}
    for key, val in record.items():
        if isinstance(val, datetime):
            out[key] = val.isoformat()
        elif isinstance(val, Decimal):
            out[key] = float(val)
        else:
            out[key] = val
    return out


# ── DB queries ────────────────────────────────────────────────────────────────

async def _latest_snapshot() -> dict:
    pool = await _get_pool()
    record = await pool.fetchrow(
        "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at DESC LIMIT 1"
    )
    if record is None:
        return {
            "id": None,
            "mode": _MODE,
            "strategy": _STRATEGY,
            "circuit_state": "closed",
            "total_value_usd": _INITIAL_CAPITAL,
            "cash_usd": _INITIAL_CAPITAL,
            "positions_value_usd": 0.0,
            "realized_pnl": 0.0,
            "open_positions": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    d = _row(record)
    d["timestamp"] = d.pop("snapshot_at")
    d.setdefault("mode", _MODE)
    d.setdefault("strategy", _STRATEGY)
    d.setdefault("circuit_state", "closed")
    return d


async def _recent_trades(limit: int = 50) -> list[dict]:
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT * FROM trades ORDER BY executed_at DESC LIMIT $1", limit
    )
    result = []
    for record in rows:
        d = _row(record)
        d["filled_size_usd"] = d.pop("size_usd", 0.0)
        d["fill_price"] = d.pop("price", 0.0)
        d["timestamp"] = d.pop("executed_at")
        d.setdefault("slippage_bps", 0)
        result.append(d)
    return result


async def _compute_metrics() -> dict:
    pool = await _get_pool()
    record = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                                     AS total_trades,
            COUNT(*) FILTER (WHERE side = 'SELL' AND size_usd > 0)      AS wins,
            COUNT(*) FILTER (WHERE side = 'SELL')                        AS total_sells,
            COALESCE(
                SUM(CASE WHEN side = 'SELL' THEN size_usd - fee_usd ELSE 0 END), 0
            )                                                            AS realized_pnl
        FROM trades
        """
    )
    d = _row(record) if record else {}
    total_sells = int(d.get("total_sells", 0))
    wins = int(d.get("wins", 0))
    return {
        "total_trades": int(d.get("total_trades", 0)),
        "win_rate": round(wins / total_sells, 4) if total_sells > 0 else 0.0,
        "realized_pnl": round(float(d.get("realized_pnl", 0.0)), 2),
        "sharpe_ratio": None,
        "max_drawdown_pct": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── WebSocket manager ─────────────────────────────────────────────────────────

class _ConnectionManager:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_manager = _ConnectionManager()

POLL_INTERVAL_SECONDS = 5


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return await _latest_snapshot()


@app.get("/api/trades")
async def trades(limit: int = 50) -> dict:
    return {"trades": await _recent_trades(limit)}


@app.get("/api/metrics")
async def metrics() -> dict:
    return await _compute_metrics()


@app.get("/api/alerts")
async def alerts() -> dict:
    return {"alerts": []}


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await _manager.connect(ws)
    last_id: Any = None
    try:
        snap = await _latest_snapshot()
        await ws.send_text(json.dumps({"type": "snapshot", **snap}))
        last_id = snap.get("id")

        while True:
            try:
                # Wait for client message (ping) with 5s timeout
                msg = await asyncio.wait_for(ws.receive_text(), timeout=POLL_INTERVAL_SECONDS)
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # Poll DB; push snapshot only when it has changed
                snap = await _latest_snapshot()
                if snap.get("id") != last_id:
                    last_id = snap.get("id")
                    await ws.send_text(json.dumps({"type": "snapshot", **snap}))
    except WebSocketDisconnect:
        pass
    finally:
        _manager.disconnect(ws)


# ── Main HTML page ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    with open(os.path.join(_static_dir, "index.html"), encoding="utf-8") as f:
        return f.read()
