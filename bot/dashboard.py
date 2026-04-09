"""
dashboard.py — Rich CLI dashboard, refreshes every DASHBOARD_REFRESH_S seconds.

Layout:
  ┌─────────────────────────────────────────────┐
  │  KALSHI EV BOT          [LIVE / PAPER MODE] │
  │  Balance: $X,XXX.XX     Daily P&L: +$XX.XX  │
  │  Win Rate: XX%          Open Positions: X/10 │
  ├─────────────────────────────────────────────┤
  │  OPEN POSITIONS                             │
  │  Ticker | Dir | Entry | Current | Edge | $  │
  ├─────────────────────────────────────────────┤
  │  LAST 5 TRADES                              │
  │  Ticker | Dir | Entry | Exit | PnL          │
  ├─────────────────────────────────────────────┤
  │  Next scan in: XX seconds                   │
  └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from bot import config
from bot import state_manager
from bot import risk_manager

console = Console()

# Shared state pushed by main loop
_current_balance: float = 0.0
_next_scan_at: float = time.monotonic()
_last_5_trades: List[dict] = []
_started_at: datetime = datetime.now(timezone.utc)
# Live bid prices for open positions (ticker → current_bid_decimal)
# Pushed by main loop during Phase 1 position management scans
_live_bids: Dict[str, float] = {}


def update_balance(balance: float) -> None:
    global _current_balance
    _current_balance = balance


def update_next_scan(next_scan_monotonic: float) -> None:
    global _next_scan_at
    _next_scan_at = next_scan_monotonic


def update_live_bid(ticker: str, bid_decimal: float) -> None:
    """Push current best bid for an open position (for unrealised P&L display)."""
    _live_bids[ticker] = bid_decimal


def clear_live_bid(ticker: str) -> None:
    """Remove bid tracking when position is closed."""
    _live_bids.pop(ticker, None)


def push_trade(trade: dict) -> None:
    """Add a completed trade to the recent-trades list (max 5)."""
    global _last_5_trades
    _last_5_trades.insert(0, trade)
    _last_5_trades = _last_5_trades[:5]


def _load_recent_trades_from_log() -> List[dict]:
    """Load last 5 trades from the JSONL log file."""
    path = config.TRADES_LOG
    if not os.path.exists(path):
        return []
    trades = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(trades) >= 5:
                break
    except OSError:
        pass
    return trades


def _compute_win_rate() -> Optional[float]:
    """Compute win rate from trades log."""
    path = config.TRADES_LOG
    if not os.path.exists(path):
        return None
    wins = total = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                    if t.get("filled"):
                        total += 1
                        if t.get("net_edge", 0) > 0:
                            wins += 1
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return (wins / total) if total > 0 else None


def _build_header() -> Panel:
    mode_text = Text()
    if config.PAPER_MODE:
        mode_text.append("◈ PAPER MODE", style="bold yellow")
    else:
        mode_text.append("◉ LIVE TRADING", style="bold green")

    daily_pnl = state_manager.get_daily_pnl()
    pnl_style = "bold green" if daily_pnl >= 0 else "bold red"
    pnl_sign = "+" if daily_pnl >= 0 else ""
    pnl_str = f"{pnl_sign}${daily_pnl:.2f}"

    win_rate = _compute_win_rate()
    win_str = f"{win_rate:.0%}" if win_rate is not None else "N/A"

    open_count = state_manager.open_position_count()
    balance_str = f"${_current_balance:,.2f}" if _current_balance else "—"
    uptime = datetime.now(timezone.utc) - _started_at
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(
        Text(f"Balance: {balance_str}", style="bold white"),
        Text(f"Daily P&L: {pnl_str}", style=pnl_style),
    )
    grid.add_row(
        Text(f"Win Rate: {win_str}", style="cyan"),
        Text(f"Open Positions: {open_count}/{config.MAX_OPEN_POSITIONS}", style="cyan"),
    )
    grid.add_row(
        Text(f"Uptime: {hours:02d}:{minutes:02d}", style="dim white"),
        mode_text,
    )

    title_text = Text("⬡ KALSHI EV BOT", style="bold bright_white")
    return Panel(grid, title=title_text, border_style="bright_blue", box=box.ROUNDED)


def _build_positions_table() -> Table:
    positions = state_manager.get_open_positions()
    table = Table(
        title="[bold]OPEN POSITIONS[/bold]",
        box=box.SIMPLE_HEAD,
        border_style="blue",
        header_style="bold cyan",
        show_lines=False,
        expand=True,
    )
    table.add_column("Ticker", style="white", no_wrap=True)
    table.add_column("Dir", style="bold", no_wrap=True, width=4)
    table.add_column("Entry", justify="right", no_wrap=True)
    table.add_column("Cur", justify="right", no_wrap=True)  # Live bid
    table.add_column("Contracts", justify="right")
    table.add_column("Unreal P&L", justify="right")          # Mark-to-market
    table.add_column("Edge", justify="right")
    table.add_column("Since", no_wrap=True)

    if not positions:
        table.add_row(
            "—", "—", "—", "—", "—", "—", "—", "—",
            style="dim",
        )
    else:
        for pos in positions:
            direction = pos.get("direction", "?")
            dir_style = "green" if direction == "YES" else "red"
            edge = pos.get("net_edge_at_entry", 0.0)
            opened_at = pos.get("opened_at", "")
            ticker = pos.get("ticker", "—")
            entry_cents = pos.get("entry_price_cents", 0)
            contracts = pos.get("contracts", 0.0)

            # Live mark-to-market unrealised P&L
            live_bid = _live_bids.get(ticker)
            if live_bid is not None and entry_cents > 0:
                upnl = (live_bid - entry_cents / 100.0) * contracts
                upnl_str = f"{'+'if upnl>=0 else ''}{upnl:.2f}"
                upnl_style = "bold green" if upnl > 0 else "bold red" if upnl < 0 else "dim"
                cur_str = f"{int(live_bid*100)}¢"
            else:
                upnl_str = "—"
                upnl_style = "dim"
                cur_str = "—"

            try:
                dt = datetime.fromisoformat(opened_at)
                since = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                since = "—"
            table.add_row(
                ticker,
                Text(direction, style=f"bold {dir_style}"),
                f"{entry_cents}¢",
                cur_str,
                f"{contracts:.1f}",
                Text(upnl_str, style=upnl_style),
                f"{edge:.1%}",
                since,
            )

    return table


def _build_trades_table() -> Table:
    trades = _load_recent_trades_from_log()
    table = Table(
        title="[bold]LAST 5 TRADES[/bold]",
        box=box.SIMPLE_HEAD,
        border_style="blue",
        header_style="bold cyan",
        show_lines=False,
        expand=True,
    )
    table.add_column("Ticker", style="white", no_wrap=True)
    table.add_column("Dir", width=4)
    table.add_column("Entry", justify="right")
    table.add_column("Edge", justify="right")
    table.add_column("Stake", justify="right")
    table.add_column("Status")
    table.add_column("Time", no_wrap=True)

    if not trades:
        table.add_row("—", "—", "—", "—", "—", "—", "—", style="dim")
    else:
        for t in trades:
            direction = t.get("direction", "?")
            dir_style = "green" if direction == "YES" else "red"
            filled = t.get("filled", False)
            status_text = Text("✓ FILLED" if filled else "✗ MISSED", style="green" if filled else "dim")
            edge = t.get("net_edge", 0.0)
            ts = t.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts)
                time_str = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_str = "—"
            table.add_row(
                t.get("ticker", "—")[:20],
                Text(direction, style=f"bold {dir_style}"),
                f"{t.get('entry_price_cents', '?')}¢",
                f"{edge:.1%}",
                f"${t.get('stake_usd', 0):.2f}",
                status_text,
                time_str,
            )

    return table


def _build_footer() -> Panel:
    now = time.monotonic()
    secs_remaining = max(0, int(_next_scan_at - now))
    risk = risk_manager.get_stats()

    status_parts = []
    if risk["halted"]:
        status_parts.append(Text("🚨 HALTED — Loss limit reached", style="bold red"))
    elif risk["can_trade"]:
        status_parts.append(Text("● Scanning active", style="bold green"))
    else:
        status_parts.append(Text("● Position cap reached", style="yellow"))

    status_parts.append(
        Text(f"  |  Next scan in: {secs_remaining}s", style="dim white")
    )
    status_parts.append(
        Text(f"  |  Paper: {'ON' if config.PAPER_MODE else 'OFF'}", style="dim yellow" if config.PAPER_MODE else "dim green")
    )

    combined = Text()
    for part in status_parts:
        combined.append_text(part)

    return Panel(combined, border_style="bright_blue", box=box.ROUNDED)


def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=7),
        Layout(name="positions", size=12),
        Layout(name="trades", size=10),
        Layout(name="footer", size=3),
    )
    layout["header"].update(_build_header())
    layout["positions"].update(Panel(_build_positions_table(), border_style="dim blue", box=box.ROUNDED))
    layout["trades"].update(Panel(_build_trades_table(), border_style="dim blue", box=box.ROUNDED))
    layout["footer"].update(_build_footer())
    return layout


async def run_dashboard() -> None:
    """
    Run the dashboard as an asyncio task.
    Refreshes every DASHBOARD_REFRESH_S seconds.
    """
    with Live(build_layout(), refresh_per_second=0.5, screen=True) as live:
        while True:
            try:
                live.update(build_layout())
            except Exception as exc:
                pass  # Dashboard errors must never crash the main loop
            await asyncio.sleep(config.DASHBOARD_REFRESH_S)


def print_snapshot() -> None:
    """Print a one-time dashboard snapshot (for paper-mode scan output)."""
    console.print(build_layout())
