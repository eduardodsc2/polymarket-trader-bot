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
from typing import TYPE_CHECKING, Callable

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

    def reconcile_onchain_balance(
        self,
        wallet_address: str,
        portfolio: PortfolioState,
    ) -> ReconciliationReport:
        """
        Stub: compares internal portfolio cash balance against on-chain USDC.e.

        In Phase 6 this method will call Blockscout MCP tools:
          - get_address_info(wallet_address, chain_id="137")
          - get_token_transfers_by_address(wallet_address, chain_id="137")
          - get_transaction_info(tx_hash, chain_id="137") for open positions

        For Phase 5 paper trading (no real wallet), returns a placeholder report
        confirming the wallet address format is valid and the network would be checked.

        Returns:
            ReconciliationReport — always ok=True in paper mode (no on-chain state).
        """
        logger.info(
            "Reconciliation stub | wallet={wallet} | internal_cash=${cash:.2f} | "
            "chain_id={chain} | contract={contract}",
            wallet=wallet_address,
            cash=portfolio.cash_usd,
            chain=POLYGON_CHAIN_ID,
            contract=USDC_E_CONTRACT,
        )

        # Phase 5: paper mode — no real transactions, balance always matches.
        return ReconciliationReport(
            wallet_address=wallet_address,
            chain_id=POLYGON_CHAIN_ID,
            checked_at=datetime.now(timezone.utc),
            onchain_usdc_balance=portfolio.cash_usd,
            internal_cash_balance=portfolio.cash_usd,
            balance_discrepancy=0.0,
            unrecorded_transfers=[],
            unconfirmed_tx_hashes=[],
            ok=True,
        )

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
