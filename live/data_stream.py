"""
Live data stream — Polymarket CLOB WebSocket.

Connects to:
  wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribes to price updates for a set of token IDs and calls
on_price_update(token_id, price) for each tick received.

Features:
  - Automatic reconnect with exponential back-off (cap: 60s)
  - Ping/keepalive to detect silent disconnects
  - Clean shutdown via asyncio.Event

Usage:
  stream = DataStream(token_ids=["0xabc...", "0xdef..."], on_price_update=my_callback)
  await stream.run()          # runs until stream.stop() is called
  stream.stop()               # signal clean shutdown
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Awaitable
from typing import Any

from loguru import logger

from config.settings import Settings


# Callback type: async (token_id, price) -> None
PriceCallback = Callable[[str, float], Awaitable[None]]


class DataStream:
    """
    Subscribes to CLOB WebSocket price updates for *token_ids*.

    Args:
        token_ids:        List of CLOB token IDs to subscribe to.
        on_price_update:  Async callback called on each price tick.
        settings:         Injected Settings instance.
    """

    def __init__(
        self,
        token_ids: list[str],
        on_price_update: PriceCallback,
        settings: Settings,
    ) -> None:
        self._token_ids       = list(token_ids)
        self._on_price_update = on_price_update
        self._settings        = settings
        self._stop_event      = asyncio.Event()
        self._connected       = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Connect and stream until stop() is called.
        Reconnects automatically on disconnect with exponential back-off.
        """
        delay = self._settings.ws_reconnect_delay_seconds
        max_delay = 60.0

        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
                delay = self._settings.ws_reconnect_delay_seconds   # reset on clean disconnect
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "WebSocket disconnected: {error} — reconnecting in {delay:.0f}s",
                    error=exc,
                    delay=delay,
                )
                self._connected = False
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay
                    )
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, max_delay)

        logger.info("DataStream stopped.")

    def stop(self) -> None:
        """Signal the stream to shut down cleanly."""
        self._stop_event.set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _connect_and_stream(self) -> None:
        """Open one WebSocket session and pump messages until disconnect."""
        import websockets  # imported here so tests can mock without a real socket

        url = self._settings.clob_ws_url
        logger.info("Connecting to CLOB WebSocket: {url}", url=url)

        async with websockets.connect(
            url,
            ping_interval=self._settings.ws_ping_interval_seconds,
            ping_timeout=self._settings.ws_ping_interval_seconds,
        ) as ws:
            self._connected = True
            logger.info(
                "WebSocket connected. Subscribing to {n} token(s).",
                n=len(self._token_ids),
            )
            await self._subscribe(ws)

            async for raw in ws:
                if self._stop_event.is_set():
                    break
                await self._handle_message(raw)

        self._connected = False

    async def _subscribe(self, ws: Any) -> None:
        """Send subscription message for all tracked token IDs."""
        msg = {
            "type": "subscribe",
            "channel": "price_change",
            "markets": self._token_ids,
        }
        await ws.send(json.dumps(msg))
        logger.debug("Sent subscription for {n} markets.", n=len(self._token_ids))

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse a WebSocket message and dispatch price updates."""
        try:
            data: dict = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Unparseable WebSocket frame: {raw!r}", raw=raw)
            return

        event_type = data.get("event_type") or data.get("type")

        if event_type == "price_change":
            await self._dispatch_price_change(data)
        elif event_type == "last_trade_price":
            token_id = data.get("asset_id") or data.get("token_id")
            price_str = data.get("price")
            if token_id and price_str is not None:
                await self._on_price_update(token_id, float(price_str))
        # Other event types (orderbook, book, tick_size_change) are ignored for now

    async def _dispatch_price_change(self, data: dict) -> None:
        """Handle a price_change event which may contain multiple assets."""
        assets = data.get("assets", [])
        if not assets and "asset_id" in data:
            assets = [data]
        for asset in assets:
            token_id = asset.get("asset_id") or asset.get("token_id")
            price_str = asset.get("price")
            if token_id and price_str is not None:
                try:
                    await self._on_price_update(token_id, float(price_str))
                except Exception as exc:
                    logger.error(
                        "Error in on_price_update callback: {error}", error=exc
                    )
