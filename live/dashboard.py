"""
Lightweight FastAPI dashboard — Phase 0 skeleton.

Serves on port 8080. Provides:
  GET /         → HTML status page
  GET /health   → JSON health check (used by Docker healthcheck)
  GET /status   → JSON portfolio/bot status (stub until Phase 5)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Polymarket Trader Bot", version="0.1.0")


@app.get("/health", tags=["system"])
async def health() -> dict:
    """Docker healthcheck endpoint."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status", tags=["system"])
async def status() -> dict:
    """Bot and portfolio status. Stub until Phase 5."""
    return {
        "bot_mode": "not_started",
        "active_strategies": [],
        "open_positions": 0,
        "total_value_usd": None,
        "realized_pnl_usd": None,
        "message": "Dashboard live. Bot not yet started (Phase 0).",
    }


@app.get("/", response_class=HTMLResponse, tags=["ui"])
async def index() -> str:
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Polymarket Trader Bot</title>
        <meta http-equiv="refresh" content="10">
        <style>
            body { font-family: monospace; background: #0d1117; color: #c9d1d9; padding: 2rem; }
            h1 { color: #58a6ff; }
            .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
                     background: #238636; color: #fff; font-size: 0.85em; }
        </style>
    </head>
    <body>
        <h1>Polymarket Trader Bot <span class="badge">Phase 0</span></h1>
        <p>Infrastructure is up. Trading bot not yet started.</p>
        <ul>
            <li><a href="/health" style="color:#58a6ff">/health</a> — health check</li>
            <li><a href="/status" style="color:#58a6ff">/status</a> — bot status</li>
            <li><a href="/docs" style="color:#58a6ff">/docs</a> — API docs</li>
        </ul>
    </body>
    </html>
    """
