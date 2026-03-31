"""
Alerting — Telegram notifications for live trading events.

Sends alerts via the Telegram Bot API (no heavy library — plain httpx POST).

Events that trigger alerts:
  - PnL drops > 2% in a day
  - Any risk rule triggered (rule + reason)
  - Circuit breaker enters OPEN state
  - Reconciliation discrepancy detected
  - New high-edge opportunity detected (optional, when edge > threshold)

Configuration (.env):
  TELEGRAM_BOT_TOKEN   — Bot API token from @BotFather
  TELEGRAM_CHAT_ID     — Target chat/channel ID

If either env var is missing, alerts are only logged (no exception raised).

Usage:
    alerter = Alerter(settings)
    await alerter.send("Circuit breaker OPEN after 3 CLOB failures")
    await alerter.alert_circuit_open("ValueBetting")
    await alerter.alert_reconciliation(report)
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from config.schemas import ReconciliationReport
from config.settings import Settings


class Alerter:
    """
    Sends Telegram messages for critical live trading events.

    All send() calls are fire-and-forget — errors are logged, never raised.

    Args:
        settings:  Injected Settings (reads telegram_bot_token + telegram_chat_id).
    """

    _API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, settings: Settings) -> None:
        self._token   = getattr(settings, "telegram_bot_token", "")
        self._chat_id = getattr(settings, "telegram_chat_id", "")
        self._enabled = bool(self._token and self._chat_id)

        if not self._enabled:
            logger.debug(
                "Telegram alerting disabled — set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env to enable."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    async def send(self, message: str) -> None:
        """
        Send a plain-text alert. Silently no-ops if Telegram is not configured.
        """
        if not self._enabled:
            logger.warning("ALERT (Telegram disabled): {msg}", msg=message)
            return
        await self._post(message)

    async def alert_circuit_open(self, strategy: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await self.send(
            f"🔴 CIRCUIT BREAKER OPEN\n"
            f"Strategy: {strategy}\n"
            f"Time: {ts}\n"
            f"Action: Order submission blocked. Auto-recovers after cooldown."
        )

    async def alert_daily_pnl_drop(
        self, pnl_pct: float, current_value: float, initial_capital: float
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await self.send(
            f"⚠️ DAILY PNL DROP\n"
            f"Drop: {pnl_pct:.1%}\n"
            f"Portfolio: ${current_value:,.2f} (started ${initial_capital:,.2f})\n"
            f"Time: {ts}"
        )

    async def alert_risk_violation(self, rule: str, reason: str, strategy: str) -> None:
        await self.send(
            f"⚠️ RISK RULE TRIGGERED\n"
            f"Rule: {rule}\n"
            f"Reason: {reason}\n"
            f"Strategy: {strategy}"
        )

    async def alert_reconciliation(self, report: ReconciliationReport) -> None:
        if report.ok:
            return
        lines = [
            f"🔴 RECONCILIATION FAILED",
            f"Wallet: {report.wallet_address[:10]}…",
            f"Discrepancy: ${report.balance_discrepancy:+.4f}",
        ]
        if report.unrecorded_transfers:
            lines.append(f"Unrecorded transfers: {len(report.unrecorded_transfers)}")
        if report.unconfirmed_tx_hashes:
            lines.append(f"Unconfirmed txs: {len(report.unconfirmed_tx_hashes)}")
        lines.append(f"Checked: {report.checked_at.strftime('%Y-%m-%d %H:%M UTC')}")
        await self.send("\n".join(lines))

    async def alert_high_edge(
        self, condition_id: str, edge: float, strategy: str
    ) -> None:
        if edge < 0.10:   # Only alert on very high edges
            return
        await self.send(
            f"✅ HIGH-EDGE OPPORTUNITY\n"
            f"Market: {condition_id[:16]}…\n"
            f"Edge: {edge:.1%}\n"
            f"Strategy: {strategy}"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _post(self, text: str) -> None:
        try:
            import httpx
            url = self._API_BASE.format(token=self._token)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "chat_id": self._chat_id,
                    "text":    text,
                    "parse_mode": "HTML",
                })
                if not resp.is_success:
                    logger.warning(
                        "Telegram API error {status}: {body}",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
        except Exception as exc:
            logger.error("Failed to send Telegram alert: {error}", error=exc)
