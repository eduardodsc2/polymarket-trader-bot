"""
Live data stream — Polymarket CLOB WebSocket.

Connects to:
  wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribes to price updates for a set of token IDs and calls
on_price_update(token_id, price) for each tick received.

Features:
  - Automatic reconnect with exponential back-off (cap: 60s)
  - Watchdog task: forces reconnect if no tick received in ws_stall_timeout_seconds
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
        self._last_tick_at: float = time.monotonic()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Connect and stream until stop() is called.

        Uses the modern websockets.asyncio.client.connect() async-iterator API
        which handles exponential back-off reconnect automatically.

        For each connection, runs two concurrent tasks via TaskGroup:
          - _read_loop: receives messages and dispatches price updates
          - _watchdog:  closes the socket if no tick arrives in stall_timeout seconds

        When either task ends (watchdog fires or read loop ends), TaskGroup
        cancels the other and the outer loop reconnects.
        """
        from websockets.asyncio.client import connect

        url = self._settings.clob_ws_url
        logger.info("Connecting to CLOB WebSocket: {url}", url=url)

        try:
            async for ws in connect(
                url,
                ping_interval=self._settings.ws_ping_interval_seconds,
                ping_timeout=self._settings.ws_ping_interval_seconds,
            ):
                if self._stop_event.is_set():
                    break

                self._connected = True
                self._last_tick_at = time.monotonic()
                logger.info(
                    "WebSocket connected. Subscribing to {n} token(s).",
                    n=len(self._token_ids),
                )
                await self._subscribe(ws)

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._read_loop(ws))
                        tg.create_task(self._watchdog(ws))
                except* asyncio.CancelledError:
                    pass
                except* Exception as eg:
                    for exc in eg.exceptions:
                        logger.warning("WebSocket task error: {e}", e=exc)
                finally:
                    self._connected = False

                if self._stop_event.is_set():
                    break

                logger.debug("WebSocket loop ended — reconnecting.")

        except asyncio.CancelledError:
            pass

        logger.info("DataStream stopped.")

    def stop(self) -> None:
        """Signal the stream to shut down cleanly."""
        self._stop_event.set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _read_loop(self, ws: Any) -> None:
        """Receive messages from the WebSocket and dispatch price updates."""
        from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
        try:
            async for raw in ws:
                if self._stop_event.is_set():
                    return
                self._last_tick_at = time.monotonic()
                await self._handle_message(raw)
        except ConnectionClosedOK:
            if not self._stop_event.is_set():
                logger.debug("WebSocket closed cleanly — reconnecting.")
        except ConnectionClosed as exc:
            logger.warning("WebSocket connection lost: {e} — reconnecting.", e=exc)

    async def _watchdog(self, ws: Any) -> None:
        """Close the WebSocket if no tick received within stall_timeout seconds."""
        timeout = self._settings.ws_stall_timeout_seconds
        while not self._stop_event.is_set():
            await asyncio.sleep(30)
            elapsed = time.monotonic() - self._last_tick_at
            if elapsed > timeout:
                logger.warning(
                    "WebSocket stall detected ({elapsed:.0f}s without tick) — forcing reconnect.",
                    elapsed=elapsed,
                )
                await ws.close()
                return

    async def _subscribe(self, ws: Any) -> None:
        """Send subscription message for all tracked token IDs.

        Polymarket CLOB WebSocket format (ws/market channel):
          {"assets_ids": ["<token_id>", ...], "type": "market"}

        The server drops connections with too many assets in one frame,
        so we batch into groups of MAX_ASSETS_PER_SUB.
        """
        MAX_ASSETS_PER_SUB = 20
        for i in range(0, len(self._token_ids), MAX_ASSETS_PER_SUB):
            batch = self._token_ids[i : i + MAX_ASSETS_PER_SUB]
            msg = {"assets_ids": batch, "type": "market"}
            await ws.send(json.dumps(msg))
        logger.debug("Sent subscription for {n} assets.", n=len(self._token_ids))

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse a WebSocket message and dispatch price updates.

        Polymarket CLOB WebSocket sends two message shapes:

        1. Initial orderbook snapshot (list of objects, one per subscribed token):
           [{"asset_id": "...", "bids": [...], "asks": [...], "timestamp": "..."}, ...]

        2. Price change event (object with price_changes list):
           {"market": "0x...", "price_changes": [
               {"asset_id": "...", "price": "0.95", "best_bid": "...", "best_ask": "..."},
               ...
           ]}
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Unparseable WebSocket frame: {raw!r}", raw=raw)
            return

        # Shape 1 — initial snapshot: list of orderbook objects
        if isinstance(data, list):
            for book in data:
                token_id = book.get("asset_id")
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                price = None
                if bids and asks:
                    try:
                        best_bid = float(bids[0]["price"])
                        best_ask = float(asks[0]["price"])
                        price = (best_bid + best_ask) / 2.0
                    except (KeyError, ValueError, IndexError):
                        pass
                if token_id and price is not None:
                    try:
                        await self._on_price_update(token_id, price)
                    except Exception as exc:
                        logger.error("Error in on_price_update (snapshot): {error}", error=exc)
            return

        # Shape 2 — price_changes event
        price_changes = data.get("price_changes")
        if price_changes:
            for change in price_changes:
                token_id = change.get("asset_id")
                price_str = change.get("price")
                if not price_str:
                    bid = change.get("best_bid")
                    ask = change.get("best_ask")
                    if bid and ask:
                        try:
                            price_str = str((float(bid) + float(ask)) / 2.0)
                        except ValueError:
                            pass
                if token_id and price_str is not None:
                    try:
                        await self._on_price_update(token_id, float(price_str))
                    except Exception as exc:
                        logger.error("Error in on_price_update (price_change): {error}", error=exc)
