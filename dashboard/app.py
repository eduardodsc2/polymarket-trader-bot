"""
FastAPI dashboard backend — Phase 5.

Endpoints:
  GET  /health              → Docker health check
  GET  /api/snapshot        → Full portfolio state (initial page load)
  GET  /api/trades          → Recent trades (last 50)
  GET  /api/metrics         → Sharpe, drawdown, win rate
  WS   /ws                  → Push stream: fills, alerts, PnL updates

Architecture:
  - Bot container writes fills + PnL to a shared in-memory queue (or Postgres LISTEN/NOTIFY in Phase 6)
  - Dashboard is read-only: it never writes to DB or calls CLOB API
  - WebSocket broadcasts: on each new fill the bot calls notify_fill(), which pushes to all connected clients
  - Dashboard container can be restarted at any time without affecting the bot
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Polymarket Dashboard", version="0.5.0")

# ── Static files ──────────────────────────────────────────────────────────────
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── In-memory state (replaced by DB in Phase 6) ───────────────────────────────

class _State:
    def __init__(self) -> None:
        self.mode: str = os.getenv("BOT_MODE", "paper")
        self.active_strategy: str = os.getenv("ACTIVE_STRATEGY", "ValueBetting")
        self.initial_capital: float = float(os.getenv("INITIAL_CAPITAL_USD", "500"))
        self.cash_usd: float = self.initial_capital
        self.realized_pnl: float = 0.0
        self.trades: deque[dict] = deque(maxlen=500)
        self.risk_alerts: list[str] = []
        self.circuit_state: str = "closed"

    def portfolio_snapshot(self) -> dict:
        total = self.cash_usd + self.realized_pnl
        return {
            "mode": self.mode,
            "strategy": self.active_strategy,
            "total_value_usd": round(total, 2),
            "cash_usd": round(self.cash_usd, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "open_positions": 0,
            "circuit_state": self.circuit_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def add_fill(self, fill: dict) -> None:
        fill.setdefault("received_at", datetime.now(timezone.utc).isoformat())
        self.trades.appendleft(fill)
        if fill.get("side") == "BUY":
            self.cash_usd = max(0.0, self.cash_usd - fill.get("filled_size_usd", 0))
        else:
            self.cash_usd += fill.get("filled_size_usd", 0)


_state = _State()


# ── WebSocket connection manager ──────────────────────────────────────────────

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


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return _state.portfolio_snapshot()


@app.get("/api/trades")
async def trades(limit: int = 50) -> dict:
    return {"trades": list(_state.trades)[:limit]}


@app.get("/api/metrics")
async def metrics() -> dict:
    """Rolling metrics computed from in-memory trades."""
    fills = list(_state.trades)
    total = len(fills)
    wins = sum(1 for f in fills if f.get("side") == "SELL" and f.get("filled_size_usd", 0) > 0)
    win_rate = wins / total if total > 0 else 0.0
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "realized_pnl": round(_state.realized_pnl, 2),
        "sharpe_ratio": None,   # requires equity time-series (Phase 6)
        "max_drawdown_pct": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/alerts")
async def alerts() -> dict:
    return {"alerts": _state.risk_alerts}


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await _manager.connect(ws)
    try:
        # Send current state immediately on connect
        await ws.send_text(json.dumps({"type": "snapshot", **_state.portfolio_snapshot()}))
        # Keep alive — receive messages (ping/pong or client commands)
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                # Acknowledge client heartbeat
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # Send keepalive so connection is not dropped by proxies
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        _manager.disconnect(ws)


# ── Internal push API (called by bot process) ─────────────────────────────────

async def notify_fill(fill: dict) -> None:
    """
    Called by the bot after each paper fill.
    Updates in-memory state and broadcasts to all WebSocket clients.
    """
    _state.add_fill(fill)
    await _manager.broadcast({
        "type": "fill",
        "fill": fill,
        "portfolio": _state.portfolio_snapshot(),
    })


async def notify_alert(alert: str) -> None:
    """Called by risk manager when a rule fires."""
    _state.risk_alerts.append(alert)
    if len(_state.risk_alerts) > 50:
        _state.risk_alerts = _state.risk_alerts[-50:]
    await _manager.broadcast({"type": "alert", "message": alert})


async def notify_circuit_state(state: str) -> None:
    """Called by executor when circuit breaker changes state."""
    _state.circuit_state = state
    await _manager.broadcast({"type": "circuit", "state": state})


# ── Main HTML page ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    with open(os.path.join(_static_dir, "index.html"), encoding="utf-8") as f:
        return f.read()
