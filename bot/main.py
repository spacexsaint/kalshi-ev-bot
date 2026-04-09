"""
main.py — Orchestration loop for the Kalshi EV Arbitrage Bot.

Startup sequence:
  1. Load .env and validate required variables
  2. Test Kalshi auth (get_balance)
  3. Test Manifold + PredictIt connectivity
  4. Load state from state_manager
  5. Reset daily tracking if needed
  6. Log startup event + Discord alert

Main loop (every SCAN_INTERVAL_SEC seconds):
  PHASE 1 — POSITION MANAGEMENT
  PHASE 2 — MARKET SCANNING
  PHASE 3 — DASHBOARD UPDATE
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiohttp
from dotenv import load_dotenv
from rich.console import Console

from bot import config
from bot import dashboard
from bot import edge_calculator
from bot import executor
from bot import fair_value
from bot import logger as bot_logger
from bot import market_matcher
from bot import risk_manager
from bot import state_manager
from bot.kalshi_client import KalshiClient


# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_log = logging.getLogger("kalshi-bot.main")
console = Console()


# ── Startup validation ─────────────────────────────────────────────────────────

def _validate_env() -> None:
    required = ["KALSHI_API_KEY", "KALSHI_PRIVATE_KEY_PATH"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        _log.warning(
            "Missing env vars: %s — authenticated endpoints will fail. "
            "Running in display-only mode.",
            ", ".join(missing),
        )


async def _startup_checks(session: aiohttp.ClientSession, client: KalshiClient) -> dict:
    """Run lightweight connectivity checks. Returns status dict."""
    results = {"kalshi": False, "manifold": False, "predictit": False}

    # Kalshi auth
    balance = await client.get_balance()
    if balance is not None:
        results["kalshi"] = True
        dashboard.update_balance(balance)
        _log.info("Kalshi auth OK. Balance: $%.2f", balance)
    else:
        _log.warning("Kalshi auth failed — running in degraded mode.")

    # Manifold + PredictIt
    source_status = await fair_value.test_connectivity(session)
    results["manifold"] = source_status.get("manifold", False)
    results["predictit"] = source_status.get("predictit", False)
    _log.info(
        "Connectivity: Manifold=%s, PredictIt=%s",
        "OK" if results["manifold"] else "FAIL",
        "OK" if results["predictit"] else "FAIL",
    )

    return results


# ── Discord helpers (delegated to executor module) ─────────────────────────────

async def _send_startup_alert(session: aiohttp.ClientSession, balance: Optional[float]) -> None:
    mode = "PAPER" if config.PAPER_MODE else "LIVE"
    bal_str = f"${balance:.2f}" if balance else "unknown"
    content = f"🟢 **Bot started in {mode} mode** — Balance: {bal_str}"
    await executor._send_discord(session, content)


async def _send_daily_summary(session: aiohttp.ClientSession) -> None:
    pnl = state_manager.get_daily_pnl()
    balance = dashboard._current_balance
    pnl_sign = "+" if pnl >= 0 else ""

    # Count bets from today's log
    today = datetime.now(timezone.utc).date().isoformat()
    n_bets = wins = 0
    try:
        import json
        with open(config.TRADES_LOG, "r") as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                    if t.get("ts", "")[:10] == today and t.get("filled"):
                        n_bets += 1
                        if t.get("net_edge", 0) > 0:
                            wins += 1
                except Exception:
                    pass
    except OSError:
        pass

    win_rate = f"{wins / n_bets:.0%}" if n_bets > 0 else "N/A"
    content = (
        f"📊 **DAILY SUMMARY**\n"
        f"P&L: **{pnl_sign}${pnl:.2f}** | Bets: {n_bets} | Win Rate: {win_rate}\n"
        f"Balance: ${balance:.2f}"
    )
    await executor._send_discord(session, content)


# ── Phase 1: Position management ───────────────────────────────────────────────

async def _manage_positions(
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> None:
    positions = state_manager.get_open_positions()
    if not positions:
        return

    _log.debug("Managing %d open positions...", len(positions))

    for pos in positions:
        ticker = pos["ticker"]
        direction = pos["direction"]
        entry_cents = pos["entry_price_cents"]

        try:
            ob = await client.get_orderbook(ticker)
            if ob is None:
                # Try fetching market to see if resolved
                market = await client.get_market(ticker)
                if market and market.get("status") == "finalized":
                    result = market.get("result", "")
                    contracts = pos["contracts"]
                    entry_decimal = entry_cents / 100.0

                    if result == "yes":
                        # YES contracts pay $1 each
                        pnl = (1.0 - entry_decimal) * contracts if direction == "YES" else -entry_decimal * contracts
                    elif result == "no":
                        pnl = -entry_decimal * contracts if direction == "YES" else (1.0 - entry_decimal) * contracts
                    else:
                        pnl = 0.0

                    await executor.close_position(
                        position=pos,
                        current_bid_cents=100 if (result == direction.lower()) else 0,
                        reason="resolved",
                        resolution_pnl=pnl,
                        client=client,
                        session=session,
                    )
                continue

            # Check profit-take condition
            current_bid = ob.yes_bid if direction == "YES" else ob.no_bid
            current_bid_cents = int(current_bid * 100)

            if current_bid_cents >= entry_cents + config.PROFIT_TAKE_CENTS:
                _log.info(
                    "Profit take on %s: entry=%dc, current_bid=%dc (+%dc)",
                    ticker, entry_cents, current_bid_cents,
                    current_bid_cents - entry_cents,
                )
                await executor.close_position(
                    position=pos,
                    current_bid_cents=current_bid_cents,
                    reason="profit_take",
                    client=client,
                    session=session,
                )

        except Exception as exc:
            _log.error("Error managing position %s: %s", ticker, exc)


# ── Phase 2: Market scanning ───────────────────────────────────────────────────

async def _evaluate_market(
    market: dict,
    client: KalshiClient,
    session: aiohttp.ClientSession,
    balance: float,
) -> Optional[dict]:
    """
    Evaluate a single market. Returns a candidate dict if edge found, else None.
    """
    ticker = market.get("ticker", "")
    title = market.get("title", "")

    # Parse close time
    close_time_str = market.get("close_time", "")
    close_dt: Optional[datetime] = None
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    # Fetch orderbook
    ob = await client.get_orderbook(ticker)
    if ob is None:
        return None

    # Liquidity filter: bid-ask spread
    spread = ob.yes_ask - ob.yes_bid
    if spread > config.MAX_BID_ASK_SPREAD:
        return None

    # Get fair value
    fv = await fair_value.get_fair_value(
        kalshi_ticker=ticker,
        kalshi_title=title,
        kalshi_close_date=close_dt,
        session=session,
    )
    if fv is None:
        return None

    # Compute edge
    if ob.yes_ask <= 0 or ob.no_ask <= 0:
        return None

    try:
        result = edge_calculator.compute_edge(
            w=fv.probability,
            p=ob.yes_ask,
            q=ob.no_ask,
            balance=balance,
            ticker=ticker,
        )
    except ValueError:
        return None

    if result.direction == "NONE" or result.net_edge < config.MIN_EDGE:
        return None

    return {
        "ticker": ticker,
        "title": title,
        "direction": result.direction,
        "price_cents": int(
            (ob.yes_ask if result.direction == "YES" else ob.no_ask) * 100
        ),
        "stake_usd": result.stake_usd,
        "net_edge": result.net_edge,
        "gross_edge": result.gross_edge,
        "kelly_fraction": result.kelly_fraction,
        "fee_usd": result.fee_usd,
        "fair_prob": fv.probability,
        "fair_prob_sources": fv.sources,
        "ob_yes_ask": ob.yes_ask,
        "ob_no_ask": ob.no_ask,
        "close_dt": close_dt,
    }


async def _scan_markets(
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> None:
    if not risk_manager.can_trade():
        _log.info("Skipping market scan — trading not allowed (circuit breaker or position cap).")
        return

    balance = await client.get_balance()
    if balance is None:
        _log.warning("Could not fetch balance; using cached value.")
        balance = dashboard._current_balance
    else:
        dashboard.update_balance(balance)
        state_manager.set_daily_start_balance(balance)

    if balance <= 0:
        _log.warning("Zero or negative balance — skipping scan.")
        return

    _log.info("Starting market scan. Balance: $%.2f", balance)

    # Fetch all open markets
    all_markets = await client.get_all_open_markets()

    now = datetime.now(timezone.utc)
    min_close = now + timedelta(hours=config.MIN_TIME_TO_CLOSE_HR)
    max_close = now + timedelta(days=config.MAX_TIME_TO_CLOSE_DAYS)
    open_tickers_set = set(state_manager.open_tickers())

    # Filter markets
    candidates_input = []
    for m in all_markets:
        ticker = m.get("ticker", "")

        # Skip already-held positions
        if ticker in open_tickers_set:
            continue

        # Volume filter
        volume = m.get("volume", 0) or 0
        if volume < config.MIN_MARKET_VOLUME:
            continue

        # Binary only (no range markets)
        market_type = m.get("market_type", "")
        if market_type and market_type.lower() not in ("binary", ""):
            continue

        # Time filter
        close_time_str = m.get("close_time", "")
        try:
            close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            if close_dt < min_close or close_dt > max_close:
                continue
        except (ValueError, AttributeError):
            pass   # If we can't parse, allow through

        candidates_input.append(m)

    _log.info("Evaluating %d candidate markets after filters...", len(candidates_input))

    # Evaluate concurrently (max CONCURRENT_MARKET_SCANS at once)
    semaphore = asyncio.Semaphore(config.CONCURRENT_MARKET_SCANS)

    async def _evaluate_with_semaphore(market: dict) -> Optional[dict]:
        async with semaphore:
            try:
                return await _evaluate_market(market, client, session, balance)
            except Exception as exc:
                _log.debug("Evaluation failed for %s: %s", market.get("ticker"), exc)
                return None

    tasks = [_evaluate_with_semaphore(m) for m in candidates_input]
    results = await asyncio.gather(*tasks)
    edges = [r for r in results if r is not None]

    if not edges:
        _log.info("No edge found across %d markets.", len(candidates_input))
        return

    # Sort by net_edge descending
    edges.sort(key=lambda x: x["net_edge"], reverse=True)
    _log.info("Found %d markets with edge. Top edge: %.1f%%", len(edges), edges[0]["net_edge"] * 100)

    # Execute top candidates
    for candidate in edges:
        if not risk_manager.can_trade():
            _log.info("Trading stopped mid-scan (circuit breaker or position cap).")
            break

        ticker = candidate["ticker"]

        # Re-fetch orderbook to confirm price hasn't moved
        ob = await client.get_orderbook(ticker)
        if ob is None:
            continue

        direction = candidate["direction"]
        original_price = candidate["price_cents"]
        fresh_price = int((ob.yes_ask if direction == "YES" else ob.no_ask) * 100)

        # Stale price check
        if abs(fresh_price - original_price) > config.PRICE_STALENESS_CENTS:
            _log.info(
                "Stale price on %s: evaluated at %dc, now %dc. Skipping.",
                ticker, original_price, fresh_price,
            )
            continue

        await executor.place_bet(
            ticker=ticker,
            market_title=candidate["title"],
            direction=direction,
            stake_usd=candidate["stake_usd"],
            price_cents=fresh_price,
            fair_prob=candidate["fair_prob"],
            gross_edge=candidate["gross_edge"],
            net_edge=candidate["net_edge"],
            kelly_fraction=candidate["kelly_fraction"],
            fee_usd=candidate["fee_usd"],
            fair_prob_sources=candidate["fair_prob_sources"],
            client=client,
            session=session,
        )

        # Small delay between orders
        await asyncio.sleep(0.5)


# ── Daily summary scheduler ────────────────────────────────────────────────────

async def _daily_summary_loop(session: aiohttp.ClientSession) -> None:
    """Send a daily summary at 11:59 PM UTC."""
    while True:
        now = datetime.now(timezone.utc)
        if (now.hour == config.DAILY_SUMMARY_UTC_HOUR and
                now.minute == config.DAILY_SUMMARY_UTC_MINUTE):
            await _send_daily_summary(session)
            await asyncio.sleep(90)   # Sleep past the minute
        else:
            await asyncio.sleep(30)


# ── Main orchestration loop ────────────────────────────────────────────────────

async def main_loop(single_cycle: bool = False) -> None:
    """
    Main bot loop.

    Args:
        single_cycle: If True, run exactly one scan cycle then return.
                      Used for paper-mode demo and testing.
    """
    load_dotenv()
    _validate_env()

    # Ensure log/data dirs exist
    os.makedirs(config.LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)

    # Load persisted state
    state_manager.load()
    market_matcher.initialise()

    async with aiohttp.ClientSession() as session:
        client = KalshiClient(session)

        # Startup checks
        startup_results = await _startup_checks(session, client)
        balance = dashboard._current_balance

        # Daily reset if needed
        if state_manager.needs_daily_reset():
            risk_manager.reset_daily(balance)

        # Startup log
        bot_logger.log_event(
            "startup",
            f"Bot started in {'PAPER' if config.PAPER_MODE else 'LIVE'} mode. Balance: ${balance:.2f}",
            extra={
                "paper_mode": config.PAPER_MODE,
                "balance_usd": balance,
                "connectivity": startup_results,
            },
        )
        await _send_startup_alert(session, balance)

        console.print(
            f"\n[bold bright_blue]⬡ KALSHI EV BOT[/bold bright_blue] "
            f"[{'bold yellow' if config.PAPER_MODE else 'bold green'}]"
            f"{'◈ PAPER MODE' if config.PAPER_MODE else '◉ LIVE TRADING'}[/]"
            f"\nBalance: [bold]\${balance:.2f}[/bold]"
            f"  |  Kalshi: {'[green]OK[/]' if startup_results['kalshi'] else '[red]FAIL[/]'}"
            f"  |  Manifold: {'[green]OK[/]' if startup_results['manifold'] else '[red]FAIL[/]'}"
            f"  |  PredictIt: {'[green]OK[/]' if startup_results['predictit'] else '[red]FAIL[/]'}\n"
        )

        if single_cycle:
            # One full scan cycle for demo/testing
            console.print("[bold]Running single scan cycle...[/bold]")
            await _manage_positions(client, session)
            await _scan_markets(client, session)
            dashboard.print_snapshot()
            return

        # Launch dashboard and daily summary as background tasks
        dashboard_task = asyncio.create_task(dashboard.run_dashboard())
        daily_task = asyncio.create_task(_daily_summary_loop(session))

        try:
            while True:
                loop_start = time.monotonic()

                # Phase 1
                await _manage_positions(client, session)

                # Phase 2
                await _scan_markets(client, session)

                # Phase 3: set next scan time for dashboard
                next_scan = time.monotonic() + config.SCAN_INTERVAL_SEC
                dashboard.update_next_scan(next_scan)

                # Sleep until next interval
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0, config.SCAN_INTERVAL_SEC - elapsed)
                _log.info("Scan complete in %.1fs. Next scan in %.0fs.", elapsed, sleep_time)
                await asyncio.sleep(sleep_time)

        except (KeyboardInterrupt, asyncio.CancelledError):
            _log.info("Shutting down...")
            dashboard_task.cancel()
            daily_task.cancel()
            bot_logger.log_event("shutdown", "Bot shut down gracefully.")
            console.print("\n[bold red]Bot stopped.[/bold red]")


def main() -> None:
    """Entry point for the bot."""
    asyncio.run(main_loop(single_cycle=False))


def run_single_cycle() -> None:
    """Run one scan cycle (used for paper-mode demo)."""
    asyncio.run(main_loop(single_cycle=True))


if __name__ == "__main__":
    import sys
    single = "--single" in sys.argv
    asyncio.run(main_loop(single_cycle=single))
