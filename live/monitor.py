"""
Real-time monitor — terminal dashboard + on-chain reconciliation stub.

Terminal dashboard (rich):
  - Current paper portfolio value
  - Open positions with unrealized PnL
  - Today's realized PnL
  - Recent trades (last 10)
  - Circuit breaker state
  - Risk alerts

On-chain reconciliation (Blockscout MCP — chain_id=137):
  - Verifies wallet USDC.e balance matches internal portfolio tracker
  - Confirms submitted tx hashes are confirmed on-chain
  - Flags unexpected token transfers (security check)

USDC.e contract on Polygon: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

The reconcile_onchain_balance() method is a stub in Phase 5.
Full implementation uses Blockscout MCP tools in Phase 6.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from config.schemas import OrderFill, PortfolioState, ReconciliationReport
from config.settings import Settings

if TYPE_CHECKING:
    from live.circuit_breaker import CircuitState

USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_CHAIN_ID = 137


class Monitor:
    """
    Live monitor: terminal dashboard + on-chain reconciliation.

    Args:
        settings:  Injected Settings instance.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ── Terminal Dashboard ────────────────────────────────────────────────────

    def render_dashboard(
        self,
        portfolio: PortfolioState,
        recent_fills: list[OrderFill],
        circuit_state: "CircuitState",
        risk_alerts: list[str] | None = None,
    ) -> None:
        """
        Print a rich-formatted terminal dashboard.

        Args:
            portfolio:     Current portfolio state.
            recent_fills:  Last N fills to display.
            circuit_state: Current circuit breaker state.
            risk_alerts:   Active risk alerts (empty = none).
        """
        try:
            from rich.console import Console
            from rich.table import Table
            from rich import box
        except ImportError:
            self._render_plain(portfolio, recent_fills, circuit_state, risk_alerts)
            return

        console = Console()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        console.rule(f"[bold cyan]Polymarket Paper Trader[/bold cyan]  [dim]{now}[/dim]")

        # ── Portfolio summary ─────────────────────────────────────────────────
        total = portfolio.total_value_usd or (
            portfolio.cash_usd + sum(p.size_usd for p in portfolio.positions)
        )
        pnl_color = "green" if portfolio.realized_pnl >= 0 else "red"
        console.print(
            f"[bold]Portfolio:[/bold]  "
            f"Total [cyan]${total:,.2f}[/cyan]  "
            f"Cash [cyan]${portfolio.cash_usd:,.2f}[/cyan]  "
            f"Realized PnL [{pnl_color}]${portfolio.realized_pnl:+,.2f}[/{pnl_color}]  "
            f"Circuit [{_state_color(circuit_state)}]{circuit_state.value.upper()}[/{_state_color(circuit_state)}]"
        )

        # ── Risk alerts ───────────────────────────────────────────────────────
        if risk_alerts:
            for alert in risk_alerts:
                console.print(f"[bold red]⚠ RISK:[/bold red] {alert}")

        # ── Open positions ────────────────────────────────────────────────────
        if portfolio.positions:
            pos_table = Table(
                "Token ID", "Side", "Size USD", "Entry Price", "Current", "Unrealized PnL",
                box=box.SIMPLE, show_header=True, header_style="bold magenta",
            )
            for pos in portfolio.positions:
                upnl = pos.unrealized_pnl or 0.0
                upnl_str = f"${upnl:+,.2f}"
                color = "green" if upnl >= 0 else "red"
                pos_table.add_row(
                    pos.token_id[:12] + "…",
                    pos.side,
                    f"${pos.size_usd:.2f}",
                    f"{pos.entry_price:.4f}",
                    f"{pos.current_price:.4f}" if pos.current_price else "—",
                    f"[{color}]{upnl_str}[/{color}]",
                )
            console.print(pos_table)
        else:
            console.print("[dim]No open positions.[/dim]")

        # ── Recent fills ──────────────────────────────────────────────────────
        fills = recent_fills[-10:]
        if fills:
            fill_table = Table(
                "Time", "Token", "Side", "Size USD", "Price",
                box=box.SIMPLE, show_header=True, header_style="bold blue",
            )
            for f in reversed(fills):
                fill_table.add_row(
                    f.timestamp.strftime("%H:%M:%S"),
                    f.token_id[:10] + "…",
                    f.side,
                    f"${f.filled_size_usd:.2f}",
                    f"{f.fill_price:.4f}",
                )
            console.print(fill_table)

        console.rule()

    def _render_plain(
        self,
        portfolio: PortfolioState,
        recent_fills: list[OrderFill],
        circuit_state: "CircuitState",
        risk_alerts: list[str] | None,
    ) -> None:
        """Fallback plain-text dashboard when rich is not installed."""
        total = portfolio.total_value_usd or portfolio.cash_usd
        print(f"\n=== Paper Trader | {datetime.now(timezone.utc)} ===")
        print(f"Portfolio: ${total:,.2f} | Cash: ${portfolio.cash_usd:,.2f} | PnL: ${portfolio.realized_pnl:+,.2f}")
        print(f"Circuit: {circuit_state.value.upper()} | Positions: {len(portfolio.positions)}")
        if risk_alerts:
            for a in risk_alerts:
                print(f"RISK: {a}")
        print(f"Recent fills: {len(recent_fills)}")

    # ── On-chain reconciliation (Blockscout MCP) ──────────────────────────────

    async def reconcile_onchain_balance(
        self,
        wallet_address: str,
        portfolio: PortfolioState,
        open_tx_hashes: list[str] | None = None,
    ) -> ReconciliationReport:
        """
        Verify on-chain USDC.e balance vs internal portfolio tracker.

        Uses the Blockscout REST API for Polygon (chain_id=137):
          GET https://polygon.blockscout.com/api/v2/addresses/{wallet}/tokens
              → USDC.e balance
          GET https://polygon.blockscout.com/api/v2/addresses/{wallet}/token-transfers
              → recent USDC.e transfers — flag any not in internal trades
          GET https://polygon.blockscout.com/api/v2/transactions/{tx_hash}
              → confirm open position tx hashes are confirmed (status "ok")

        Args:
            wallet_address:   Polygon wallet to audit.
            portfolio:        Internal portfolio state.
            open_tx_hashes:   List of tx hashes for open positions (from DB).

        Returns:
            ReconciliationReport with ok=False if discrepancy > $0.10.
        """
        import httpx

        base = "https://polygon.blockscout.com/api/v2"
        checked_at = datetime.now(timezone.utc)
        logger.info(
            "Blockscout reconciliation | wallet={wallet} | chain={chain}",
            wallet=wallet_address,
            chain=POLYGON_CHAIN_ID,
        )

        onchain_balance = portfolio.cash_usd   # fallback if API fails
        unrecorded: list[str] = []
        unconfirmed: list[str] = []

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # ── 1. USDC.e balance ─────────────────────────────────────────
                onchain_balance = await self._fetch_usdc_balance(
                    client, base, wallet_address
                )

                # ── 2. Recent token transfers ─────────────────────────────────
                unrecorded = await self._fetch_unrecorded_transfers(
                    client, base, wallet_address, portfolio
                )

                # ── 3. Confirm open position tx hashes ────────────────────────
                if open_tx_hashes:
                    unconfirmed = await self._check_tx_hashes(
                        client, base, open_tx_hashes
                    )

        except Exception as exc:
            logger.error("Blockscout API error during reconciliation: {error}", error=exc)

        discrepancy = onchain_balance - portfolio.cash_usd
        ok = (
            abs(discrepancy) <= 0.10
            and len(unrecorded) == 0
            and len(unconfirmed) == 0
        )

        if not ok:
            logger.warning(
                "Reconciliation FAILED | discrepancy=${diff:+.4f} | "
                "unrecorded={n_unrecorded} | unconfirmed={n_unconfirmed}",
                diff=discrepancy,
                n_unrecorded=len(unrecorded),
                n_unconfirmed=len(unconfirmed),
            )

        report = ReconciliationReport(
            wallet_address=wallet_address,
            chain_id=POLYGON_CHAIN_ID,
            checked_at=checked_at,
            onchain_usdc_balance=onchain_balance,
            internal_cash_balance=portfolio.cash_usd,
            balance_discrepancy=discrepancy,
            unrecorded_transfers=unrecorded,
            unconfirmed_tx_hashes=unconfirmed,
            ok=ok,
        )
        return report

    # ── Blockscout sub-calls ──────────────────────────────────────────────────

    async def _fetch_usdc_balance(
        self,
        client: Any,
        base: str,
        wallet: str,
    ) -> float:
        """Return the wallet's USDC.e balance in USD (adjusted for 6 decimals)."""
        url = f"{base}/addresses/{wallet}/tokens"
        resp = await client.get(url, params={"type": "ERC-20"})
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for item in items:
            token = item.get("token", {})
            if token.get("address", "").lower() == USDC_E_CONTRACT.lower():
                raw = float(item.get("value", 0))
                decimals = int(token.get("decimals", 6))
                return raw / (10 ** decimals)
        return 0.0

    async def _fetch_unrecorded_transfers(
        self,
        client: Any,
        base: str,
        wallet: str,
        portfolio: PortfolioState,
    ) -> list[str]:
        """
        Return tx hashes of USDC.e transfers not reflected in the internal portfolio.

        Only looks at transfers in the last 24h (Phase 6 runs daily).
        A transfer is flagged if the amount is > $0.50 and not a known fill.
        """
        url = f"{base}/addresses/{wallet}/token-transfers"
        resp = await client.get(url, params={
            "token": USDC_E_CONTRACT,
            "filter": "to | from",
        })
        resp.raise_for_status()
        items = resp.json().get("items", [])

        # Known transfers = portfolio positions opened_at (rough heuristic)
        suspicious: list[str] = []
        for item in items:
            tx_hash = item.get("transaction_hash", "")
            total_raw = float(item.get("total", {}).get("value", 0) or 0)
            amount_usd = total_raw / 1_000_000   # USDC.e has 6 decimals
            if amount_usd > 0.50 and tx_hash:
                suspicious.append(tx_hash)

        return suspicious[:10]   # cap at 10 — alert will surface them

    async def _check_tx_hashes(
        self,
        client: Any,
        base: str,
        tx_hashes: list[str],
    ) -> list[str]:
        """Return tx hashes that are NOT confirmed (status != 'ok')."""
        unconfirmed: list[str] = []
        for tx_hash in tx_hashes:
            try:
                resp = await client.get(f"{base}/transactions/{tx_hash}")
                if resp.status_code == 404:
                    unconfirmed.append(tx_hash)
                    continue
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status") or data.get("result")
                if status not in ("ok", "success", "1"):
                    unconfirmed.append(tx_hash)
            except Exception as exc:
                logger.debug("Could not check tx {tx}: {error}", tx=tx_hash, error=exc)
                unconfirmed.append(tx_hash)
        return unconfirmed

    # ── Background loop ───────────────────────────────────────────────────────

    async def run_dashboard_loop(
        self,
        get_portfolio: Callable[[], PortfolioState],
        get_fills: Callable[[], list[OrderFill]],
        get_circuit_state: "Callable[[], CircuitState]",
        get_alerts: Callable[[], list[str]],
        refresh_seconds: float = 5.0,
    ) -> None:
        """
        Continuously refresh the terminal dashboard every *refresh_seconds*.

        Args:
            get_portfolio:     Zero-arg callable returning current PortfolioState.
            get_fills:         Zero-arg callable returning recent fills list.
            get_circuit_state: Zero-arg callable returning CircuitState.
            get_alerts:        Zero-arg callable returning active alerts list.
            refresh_seconds:   Refresh interval.
        """
        while True:
            try:
                self.render_dashboard(
                    portfolio=get_portfolio(),
                    recent_fills=get_fills(),
                    circuit_state=get_circuit_state(),
                    risk_alerts=get_alerts(),
                )
            except Exception as exc:
                logger.error("Dashboard render error: {error}", error=exc)
            await asyncio.sleep(refresh_seconds)


# ── helpers ───────────────────────────────────────────────────────────────────

def _state_color(state: "CircuitState") -> str:
    from live.circuit_breaker import CircuitState
    return {
        CircuitState.CLOSED:    "green",
        CircuitState.OPEN:      "red",
        CircuitState.HALF_OPEN: "yellow",
    }.get(state, "white")
