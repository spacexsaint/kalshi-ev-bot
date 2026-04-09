"""
backtest.py — Backtesting engine for the Kalshi EV bot strategy.

Attempts to fetch Kalshi historical market data via API.
If unavailable, generates a synthetic dataset of 500 binary market outcomes
with realistic price distributions.

Output:
  - Total return %
  - Sharpe ratio
  - Max drawdown
  - Win rate
  - Average edge
  - Average hold time
  - P&L curve (ASCII chart via rich)

Usage:
    python backtest.py
    python backtest.py --synthetic   (force synthetic data)
    python backtest.py --n 1000      (synthetic: use 1000 markets)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from bot import config
from bot import fee_calculator
from bot import edge_calculator

load_dotenv()
console = Console()

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class HistoricalMarket:
    ticker: str
    title: str
    yes_price: float          # Entry YES ask price (decimal)
    no_price: float           # Entry NO ask price (decimal)
    fair_prob: float          # Simulated external fair probability
    resolved_yes: bool        # True if market resolved YES
    volume: int
    hold_hours: float         # How long position was held


@dataclass
class BacktestTrade:
    ticker: str
    direction: str            # "YES" | "NO"
    entry_price: float
    contracts: float
    stake_usd: float
    fee_usd: float
    net_edge: float
    gross_edge: float
    resolved_yes: bool
    pnl_usd: float
    hold_hours: float


@dataclass
class BacktestResult:
    trades: List[BacktestTrade]
    starting_balance: float
    ending_balance: float
    equity_curve: List[float]
    timestamps: List[int]      # Relative hours

    @property
    def total_return_pct(self) -> float:
        if self.starting_balance == 0:
            return 0.0
        return (self.ending_balance - self.starting_balance) / self.starting_balance

    @property
    def win_rate(self) -> float:
        filled = [t for t in self.trades if t.stake_usd > 0]
        if not filled:
            return 0.0
        wins = sum(1 for t in filled if t.pnl_usd > 0)
        return wins / len(filled)

    @property
    def sharpe_ratio(self) -> float:
        """Compute annualised Sharpe from per-trade returns."""
        if len(self.trades) < 2:
            return 0.0
        returns = [t.pnl_usd / max(t.stake_usd, 0.01) for t in self.trades if t.stake_usd > 0]
        if not returns:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.001
        # Annualise assuming ~1 trade per 5 min scan (105,120 per year)
        trades_per_year = 105120
        return (mean_r / std_r) * math.sqrt(min(len(returns), trades_per_year))

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def avg_edge(self) -> float:
        edges = [t.net_edge for t in self.trades]
        return sum(edges) / len(edges) if edges else 0.0

    @property
    def avg_hold_hours(self) -> float:
        holds = [t.hold_hours for t in self.trades]
        return sum(holds) / len(holds) if holds else 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)


# ── Data loading ───────────────────────────────────────────────────────────────

def _try_fetch_kalshi_historical() -> Optional[List[HistoricalMarket]]:
    """
    Attempt to fetch historical settled markets from Kalshi API.
    Returns None if API is unavailable or credentials are missing.
    """
    api_key = os.getenv("KALSHI_API_KEY", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

    if not api_key or not key_path or not os.path.exists(key_path):
        return None

    console.print("[dim]Attempting to fetch Kalshi historical data...[/dim]")

    try:
        import base64
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding

        with open(key_path, "rb") as fh:
            private_key = serialization.load_pem_private_key(
                fh.read(), password=None, backend=default_backend()
            )

        base_url = config.KALSHI_BASE_URL_PROD
        path = "/markets"
        ts_ms = str(int(time.time() * 1000))
        msg = f"{ts_ms}GET{path}".encode("utf-8")
        sig = base64.b64encode(
            private_key.sign(
                msg,
                crypto_padding.PSS(
                    mgf=crypto_padding.MGF1(hashes.SHA256()),
                    salt_length=crypto_padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        ).decode("utf-8")

        headers = {
            "KALSHI-ACCESS-KEY": api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }
        resp = requests.get(
            f"{base_url}{path}",
            headers=headers,
            params={"status": "finalized", "limit": 500},
            timeout=15,
        )
        if resp.status_code != 200:
            console.print(f"[yellow]Historical fetch returned HTTP {resp.status_code}. Using synthetic data.[/yellow]")
            return None

        data = resp.json()
        markets_raw = data.get("markets", [])
        if not markets_raw:
            return None

        markets = []
        for m in markets_raw:
            result = m.get("result", "")
            if result not in ("yes", "no"):
                continue
            # Use last price as a proxy for fair value entry
            last_price = m.get("last_price", 0.5) or 0.5
            entry_yes = max(0.01, min(0.99, float(last_price) + 0.03))
            entry_no = max(0.01, min(0.99, 1.0 - float(last_price) + 0.03))
            fair_prob = float(last_price)
            markets.append(HistoricalMarket(
                ticker=m.get("ticker", ""),
                title=m.get("title", "")[:80],
                yes_price=entry_yes,
                no_price=entry_no,
                fair_prob=fair_prob,
                resolved_yes=(result == "yes"),
                volume=m.get("volume", 0) or 0,
                hold_hours=random.uniform(2, 72),
            ))

        console.print(f"[green]Loaded {len(markets)} historical Kalshi markets.[/green]")
        return markets if markets else None

    except Exception as exc:
        console.print(f"[yellow]Historical fetch failed: {exc}. Using synthetic data.[/yellow]")
        return None


def _generate_synthetic_markets(n: int = 500, seed: int = 42) -> List[HistoricalMarket]:
    """
    Generate a synthetic dataset of binary market outcomes with realistic distributions.

    Price distribution: skewed towards extremes (markets often resolve near certainty).
    Fair probability: uniformly distributed with small edge injected.
    """
    random.seed(seed)
    markets = []

    for i in range(n):
        # Fair probability: uniform with slight bias toward mid-range
        fair_prob = random.betavariate(2, 2)  # Beta(2,2) → peaked at 0.5
        fair_prob = max(0.05, min(0.95, fair_prob))

        # Market price: fair ± noise (market maker uncertainty)
        noise = random.gauss(0, 0.04)
        yes_price = max(0.01, min(0.98, fair_prob + noise))
        no_price = max(0.01, min(0.98, 1.0 - fair_prob + random.gauss(0, 0.02)))

        # Ensure bid-ask spread is reasonable
        yes_price = round(yes_price, 2)
        no_price = round(no_price, 2)

        # Resolution: based on fair probability + some randomness
        resolved_yes = random.random() < fair_prob

        # Volume: log-normal
        volume = int(math.exp(random.gauss(9, 1.5)))   # ~$8k median
        volume = max(5000, volume)

        # Hold time: uniform 2–72 hours
        hold_hours = random.uniform(2, 72)

        markets.append(HistoricalMarket(
            ticker=f"SYNTH-{i:04d}",
            title=f"Synthetic market #{i}",
            yes_price=yes_price,
            no_price=no_price,
            fair_prob=fair_prob,
            resolved_yes=resolved_yes,
            volume=volume,
            hold_hours=hold_hours,
        ))

    return markets


# ── Backtesting engine ─────────────────────────────────────────────────────────

def _run_backtest(
    markets: List[HistoricalMarket],
    starting_balance: float = 500.0,
) -> BacktestResult:
    """
    Simulate the full EV bot strategy against historical/synthetic market data.

    Applies:
    - Edge filter (MIN_EDGE = 5%)
    - Kelly sizing with KELLY_FRACTION = 0.25
    - MAX_BET_PCT and MIN_BET_USD caps
    - Taker fee deduction
    - MAX_OPEN_POSITIONS cap (simplified: process serially)
    - DAILY_LOSS_LIMIT_PCT circuit breaker
    """
    balance = starting_balance
    equity_curve = [balance]
    timestamps = [0]
    trades: List[BacktestTrade] = []

    daily_start_balance = balance
    daily_pnl = 0.0
    simulated_hour = 0

    for mkt in markets:
        simulated_hour += int(mkt.hold_hours)

        # Daily reset (every 24 sim hours)
        if simulated_hour % 24 < mkt.hold_hours:
            daily_start_balance = balance
            daily_pnl = 0.0

        # Circuit breaker
        if daily_pnl <= -(config.DAILY_LOSS_LIMIT_PCT * daily_start_balance):
            continue

        # Position cap (simplified: max 10 concurrent = skip some)
        if len([t for t in trades[-10:] if t.pnl_usd == 0]) >= config.MAX_OPEN_POSITIONS:
            continue

        # Volume filter
        if mkt.volume < config.MIN_MARKET_VOLUME:
            continue

        # Compute edge
        try:
            result = edge_calculator.compute_edge(
                w=mkt.fair_prob,
                p=mkt.yes_price,
                q=mkt.no_price,
                balance=balance,
                ticker=mkt.ticker,
            )
        except ValueError:
            continue

        if result.direction == "NONE" or result.net_edge < config.MIN_EDGE:
            continue

        direction = result.direction
        entry_price = result.market_price
        stake_usd = result.stake_usd
        kelly_f = result.kelly_fraction

        # Sizing
        num_contracts = math.floor(stake_usd / entry_price)
        if num_contracts < 1:
            continue

        actual_stake = num_contracts * entry_price
        fee = fee_calculator.compute_taker_fee(entry_price, num_contracts, mkt.ticker)

        # Simulate resolution
        resolved_yes = mkt.resolved_yes
        if direction == "YES":
            gross_pnl = (1.0 - entry_price) * num_contracts if resolved_yes else -entry_price * num_contracts
        else:
            gross_pnl = (1.0 - entry_price) * num_contracts if not resolved_yes else -entry_price * num_contracts

        pnl_usd = gross_pnl - fee
        balance += pnl_usd
        balance = max(0.0, balance)
        daily_pnl += pnl_usd

        trade = BacktestTrade(
            ticker=mkt.ticker,
            direction=direction,
            entry_price=entry_price,
            contracts=float(num_contracts),
            stake_usd=actual_stake,
            fee_usd=fee,
            net_edge=result.net_edge,
            gross_edge=result.gross_edge,
            resolved_yes=resolved_yes,
            pnl_usd=pnl_usd,
            hold_hours=mkt.hold_hours,
        )
        trades.append(trade)
        equity_curve.append(balance)
        timestamps.append(simulated_hour)

    return BacktestResult(
        trades=trades,
        starting_balance=starting_balance,
        ending_balance=balance,
        equity_curve=equity_curve,
        timestamps=timestamps,
    )


# ── ASCII equity curve ─────────────────────────────────────────────────────────

def _ascii_equity_curve(curve: List[float], width: int = 60, height: int = 12) -> str:
    """Render an ASCII P&L curve."""
    if not curve or len(curve) < 2:
        return "[no data]"

    min_v = min(curve)
    max_v = max(curve)
    span = max_v - min_v if max_v != min_v else 1.0

    # Downsample to width
    step = max(1, len(curve) // width)
    sampled = curve[::step][:width]

    lines = []
    for row in range(height - 1, -1, -1):
        threshold = min_v + (row / (height - 1)) * span
        line = ""
        for val in sampled:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        if row == height - 1:
            prefix = f"${max_v:>8.0f} │"
        elif row == 0:
            prefix = f"${min_v:>8.0f} │"
        else:
            prefix = f"{'':>9} │"
        lines.append(prefix + line)

    lines.append(" " * 10 + "└" + "─" * len(sampled))
    lines.append(f"  Start         {'End':>52}")
    return "\n".join(lines)


# ── Report rendering ───────────────────────────────────────────────────────────

def _render_report(result: BacktestResult, data_source: str) -> None:
    console.print()
    console.rule("[bold bright_blue]⬡ KALSHI EV BOT — BACKTEST REPORT[/bold bright_blue]")
    console.print(f"[dim]Data source: {data_source} | Trades evaluated: {result.n_trades}[/dim]\n")

    # Summary table
    summary = Table(
        title="Performance Summary",
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=True,
    )
    summary.add_column("Metric", style="white", width=30)
    summary.add_column("Value", justify="right", style="bold")

    return_pct = result.total_return_pct
    return_color = "green" if return_pct >= 0 else "red"
    pnl_color = "green" if result.ending_balance >= result.starting_balance else "red"

    summary.add_row("Starting Balance", f"${result.starting_balance:,.2f}")
    summary.add_row("Ending Balance", Text(f"${result.ending_balance:,.2f}", style=f"bold {pnl_color}"))
    summary.add_row("Total P&L", Text(
        f"${result.ending_balance - result.starting_balance:+,.2f}",
        style=f"bold {pnl_color}",
    ))
    summary.add_row("Total Return", Text(f"{return_pct:.2%}", style=f"bold {return_color}"))
    summary.add_row("Sharpe Ratio", f"{result.sharpe_ratio:.3f}")
    summary.add_row("Max Drawdown", Text(f"{result.max_drawdown:.2%}", style="bold red" if result.max_drawdown > 0.15 else "bold yellow"))
    summary.add_row("Win Rate", f"{result.win_rate:.1%}")
    summary.add_row("Total Trades", str(result.n_trades))
    summary.add_row("Average Net Edge", f"{result.avg_edge:.2%}")
    summary.add_row("Average Hold Time", f"{result.avg_hold_hours:.1f}h")

    console.print(summary)
    console.print()

    # Equity curve
    if result.equity_curve:
        curve_str = _ascii_equity_curve(result.equity_curve)
        console.print(Panel(
            curve_str,
            title="[bold]P&L Curve[/bold]",
            border_style="bright_blue",
            box=box.ROUNDED,
        ))

    # Top 5 trades
    if result.trades:
        top_trades = sorted(result.trades, key=lambda t: abs(t.pnl_usd), reverse=True)[:5]
        trades_table = Table(
            title="Top 5 Trades by |PnL|",
            box=box.SIMPLE_HEAD,
            border_style="blue",
            header_style="bold cyan",
        )
        trades_table.add_column("Ticker")
        trades_table.add_column("Dir", width=4)
        trades_table.add_column("Entry", justify="right")
        trades_table.add_column("Contracts", justify="right")
        trades_table.add_column("Edge", justify="right")
        trades_table.add_column("Fee", justify="right")
        trades_table.add_column("P&L", justify="right")

        for t in top_trades:
            pnl_color = "green" if t.pnl_usd >= 0 else "red"
            trades_table.add_row(
                t.ticker[:20],
                Text(t.direction, style="green" if t.direction == "YES" else "red"),
                f"{t.entry_price:.2f}",
                f"{t.contracts:.0f}",
                f"{t.net_edge:.1%}",
                f"${t.fee_usd:.3f}",
                Text(f"${t.pnl_usd:+.2f}", style=f"bold {pnl_color}"),
            )
        console.print(trades_table)

    console.print()
    console.rule("[dim]End of Backtest[/dim]")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi EV Bot Backtester")
    parser.add_argument("--synthetic", action="store_true", help="Force synthetic data")
    parser.add_argument("--n", type=int, default=500, help="Number of synthetic markets")
    parser.add_argument("--balance", type=float, default=500.0, help="Starting balance in USD")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for synthetic data")
    args = parser.parse_args()

    if not args.synthetic:
        markets = _try_fetch_kalshi_historical()
        if markets:
            data_source = f"Kalshi historical API ({len(markets)} markets)"
        else:
            console.print("[yellow]Using synthetic dataset.[/yellow]")
            markets = _generate_synthetic_markets(n=args.n, seed=args.seed)
            data_source = f"Synthetic ({len(markets)} markets, seed={args.seed})"
    else:
        markets = _generate_synthetic_markets(n=args.n, seed=args.seed)
        data_source = f"Synthetic ({len(markets)} markets, seed={args.seed})"

    console.print(f"[dim]Running backtest on {len(markets)} markets...[/dim]")
    result = _run_backtest(markets, starting_balance=args.balance)
    _render_report(result, data_source)


if __name__ == "__main__":
    main()
