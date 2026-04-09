"""
main.py — Orchestration loop for the Kalshi EV Arbitrage Bot.

AUDIT IMPROVEMENTS (2026-04-08):
  - Updated to pass bid prices to edge_calculator for midpoint pricing
  - risk_manager.can_trade() now returns (bool, reason)
  - Polymarket connectivity added to startup check
  - source_count, uncertainty_mult, time_decay_mult threaded through
  - hours_to_close passed to edge_calculator for time-decay
  - correlation category passed to place_bet
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_log = logging.getLogger("kalshi-bot.main")
console = Console()


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
    results = {"kalshi": False, "manifold": False, "predictit": False, "polymarket": False}

    balance = await client.get_balance()
    if balance is not None:
        results["kalshi"] = True
        dashboard.update_balance(balance)
        _log.info("Kalshi auth OK. Balance: $%.2f", balance)
    else:
        _log.warning("Kalshi auth failed — running in degraded mode.")

    # Reconcile local state with Kalshi API positions.
    # If the bot crashed between placing an order and recording the position,
    # there could be orphan positions on Kalshi with no local tracking.
    # Detect and log them so the operator can intervene.
    if results["kalshi"]:
        await _reconcile_positions(client)

    source_status = await fair_value.test_connectivity(session)
    results.update(source_status)
    _log.info(
        "Connectivity: Manifold=%s, PredictIt=%s, Polymarket=%s",
        "OK" if results.get("manifold") else "FAIL",
        "OK" if results.get("predictit") else "FAIL",
        "OK" if results.get("polymarket") else "FAIL",
    )
    return results


async def _reconcile_positions(client: KalshiClient) -> None:
    """
    Compare local state positions with Kalshi API positions on startup.

    Logs warnings for:
    - Orphan positions: on Kalshi but not in local state (crash mid-trade)
    - Ghost positions: in local state but not on Kalshi (resolved/cancelled while offline)

    Does NOT auto-fix — logging only, so the operator can decide what to do.
    """
    try:
        api_positions = await client.get_positions()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, OSError) as exc:
        _log.warning("Position reconciliation failed: %s", exc)
        return

    api_tickers = set()
    for pos in api_positions:
        ticker = pos.get("ticker", "")
        if ticker:
            api_tickers.add(ticker)

    local_tickers = set(state_manager.open_tickers())

    # Orphans: on Kalshi but not locally tracked
    orphans = api_tickers - local_tickers
    if orphans:
        _log.warning(
            "POSITION RECONCILIATION: %d orphan positions on Kalshi not in local state: %s",
            len(orphans), ", ".join(sorted(orphans)),
        )
        bot_logger.log_event(
            "position_reconciliation",
            f"Found {len(orphans)} orphan positions on Kalshi: {', '.join(sorted(orphans))}",
            extra={"orphan_tickers": sorted(orphans)},
            severity="warning",
        )

    # Ghosts: in local state but not on Kalshi (resolved/cancelled while offline)
    ghosts = local_tickers - api_tickers
    if ghosts:
        _log.warning(
            "POSITION RECONCILIATION: %d ghost positions in local state not on Kalshi: %s. "
            "Removing from local state.",
            len(ghosts), ", ".join(sorted(ghosts)),
        )
        for ghost_ticker in ghosts:
            state_manager.remove_position_by_ticker(ghost_ticker)
        bot_logger.log_event(
            "position_reconciliation",
            f"Removed {len(ghosts)} ghost positions from local state: {', '.join(sorted(ghosts))}",
            extra={"ghost_tickers": sorted(ghosts)},
            severity="warning",
        )

    if not orphans and not ghosts:
        _log.info("Position reconciliation: local state matches Kalshi API (%d positions).", len(local_tickers))


async def _send_startup_alert(session: aiohttp.ClientSession, balance: Optional[float]) -> None:
    mode = "PAPER" if config.PAPER_MODE else "LIVE"
    bal_str = f"${balance:.2f}" if balance else "unknown"
    await executor._send_discord(session, f"🟢 **Bot started in {mode} mode** — Balance: {bal_str}")


async def _send_daily_summary(session: aiohttp.ClientSession) -> None:
    import json as _json
    pnl = state_manager.get_daily_pnl()
    balance = dashboard._current_balance
    today = datetime.now(timezone.utc).date().isoformat()
    n_bets = wins = 0
    try:
        with open(config.TRADES_LOG, "r") as fh:
            for line in fh:
                try:
                    t = _json.loads(line)
                    if t.get("ts", "")[:10] == today and t.get("filled"):
                        n_bets += 1
                        if t.get("net_edge", 0) > 0:
                            wins += 1
                except Exception:
                    pass
    except OSError:
        pass
    win_rate = f"{wins / n_bets:.0%}" if n_bets > 0 else "N/A"
    pnl_sign = "+" if pnl >= 0 else ""
    await executor._send_discord(
        session,
        f"📊 **DAILY SUMMARY**\n"
        f"P&L: **{pnl_sign}${pnl:.2f}** | Bets: {n_bets} | Win Rate: {win_rate}\n"
        f"Balance: ${balance:.2f}",
    )


# ── Phase 1: Position management ───────────────────────────────────────────────

async def _manage_positions(client: KalshiClient, session: aiohttp.ClientSession) -> None:
    positions = state_manager.get_open_positions()
    if not positions:
        return

    for pos in positions:
        ticker = pos["ticker"]
        direction = pos["direction"]
        entry_cents = pos["entry_price_cents"]

        try:
            ob = await client.get_orderbook(ticker)
            if ob is None:
                market = await client.get_market(ticker)
                if market and market.get("status") == "finalized":
                    result = market.get("result", "")
                    contracts = pos["contracts"]
                    entry_decimal = entry_cents / 100.0
                    if result == "yes":
                        pnl = (1.0 - entry_decimal) * contracts if direction == "YES" else -entry_decimal * contracts
                        resolved_yes = True
                    elif result == "no":
                        pnl = -entry_decimal * contracts if direction == "YES" else (1.0 - entry_decimal) * contracts
                        resolved_yes = False
                    else:
                        pnl = 0.0
                        resolved_yes = None  # Ambiguous/annulled

                    await executor.close_position(
                        position=pos,
                        current_bid_cents=100 if (result == direction.lower()) else 0,
                        reason="resolved",
                        resolution_pnl=pnl,
                        client=client,
                        session=session,
                    )
                    dashboard.clear_live_bid(ticker)

                    # Log Brier score for calibration tracking
                    if resolved_yes is not None:
                        fair_prob = pos.get("fair_prob_at_entry", 0.5)
                        brier = (fair_prob - float(resolved_yes)) ** 2
                        bot_logger.log_brier_score(
                            ticker=ticker,
                            market_title=pos.get("market_title", ticker),
                            fair_prob_at_entry=fair_prob,
                            sources=pos.get("sources", []),
                            resolved_yes=resolved_yes,
                            brier_score=brier,
                        )
                continue

            current_bid = ob.yes_bid if direction == "YES" else ob.no_bid
            current_bid_cents = int(current_bid * 100)

            # Push live bid to dashboard for mark-to-market unrealised P&L
            dashboard.update_live_bid(ticker, current_bid)

            # Profit-take: bid moved +PROFIT_TAKE_CENTS in our favour
            if current_bid_cents >= entry_cents + config.PROFIT_TAKE_CENTS:
                _log.info(
                    "Profit take on %s: entry=%dc, bid=%dc (+%dc)",
                    ticker, entry_cents, current_bid_cents, current_bid_cents - entry_cents,
                )
                await executor.close_position(
                    position=pos,
                    current_bid_cents=current_bid_cents,
                    reason="profit_take",
                    client=client,
                    session=session,
                )
                continue

            # Relative stop-loss: 40% drop from entry OR 20c absolute, whichever is LARGER.
            # Using max() makes the stop more permissive (less aggressive), which prevents
            # stopping out low-priced positions on noise.
            # For entry=20c: relative_stop=12c, absolute_stop=0c, effective_stop=12c.
            # For entry=80c: relative_stop=48c, absolute_stop=60c, effective_stop=60c.
            relative_stop_cents = int(entry_cents * (1.0 - config.STOP_LOSS_FRACTION))
            absolute_stop_cents = entry_cents - config.STOP_LOSS_CENTS
            stop_price_cents = max(relative_stop_cents, absolute_stop_cents)
            if current_bid_cents < stop_price_cents:
                _log.info(
                    "Stop-loss on %s: entry=%dc, bid=%dc, stop=%dc (rel=%dc, abs=%dc)",
                    ticker, entry_cents, current_bid_cents, stop_price_cents,
                    relative_stop_cents, absolute_stop_cents,
                )
                await executor.close_position(
                    position=pos,
                    current_bid_cents=current_bid_cents,
                    reason="stop_loss",
                    client=client,
                    session=session,
                )

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, OSError) as exc:
            _log.error("Error managing position %s: %s", ticker, exc)


# ── Pure arbitrage ─────────────────────────────────────────────────────────────

async def _place_arb_trade(
    ticker: str,
    yes_ask: float,
    no_ask: float,
    balance: float,
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> None:
    """
    Place a riskless arbitrage trade: buy both YES and NO when yes_ask + no_ask < 1.0.

    Guaranteed profit per pair = 1.0 - yes_ask - no_ask.
    Respects MAX_OPEN_POSITIONS risk limit.
    """
    # Check position cap
    tradeable, reason = risk_manager.can_trade()
    if not tradeable:
        _log.info("Arb skipped (risk): %s", reason)
        return

    # Skip arb if we already hold a position on this market — partial exposure
    # makes the "riskless" arb not actually riskless
    if state_manager.get_position(ticker) is not None:
        _log.info("Arb skipped: already hold position on %s", ticker)
        return

    cost_per_pair = yes_ask + no_ask
    # Use half of normal max bet sizing since we're buying 2 sides
    arb_budget = config.MAX_BET_PCT * balance / 2.0

    # Include per-pair fees in the denominator so total cost never exceeds budget.
    # Fee per pair = fee(yes_ask, 1) + fee(no_ask, 1); total cost = n * (cost_per_pair + fee_per_pair).
    from bot.fee_calculator import compute_taker_fee
    fee_per_pair = compute_taker_fee(yes_ask, 1, ticker) + compute_taker_fee(no_ask, 1, ticker)
    cost_with_fees = cost_per_pair + fee_per_pair
    n = math.floor(arb_budget / cost_with_fees) if cost_with_fees > 0 else 0

    if n < 1:
        _log.info("Arb too small: budget=%.2f, cost_per_pair=%.2f", arb_budget, cost_per_pair)
        return

    # Subtract actual fees from guaranteed profit — arb is NOT riskless if fees exceed spread
    yes_fee = compute_taker_fee(yes_ask, n, ticker)
    no_fee = compute_taker_fee(no_ask, n, ticker)
    spread = 1.0 - cost_per_pair
    guaranteed_profit = spread * n - yes_fee - no_fee

    if guaranteed_profit <= 0:
        _log.info(
            "Arb unprofitable after fees: spread=%.4f, n=%d, yes_fee=%.4f, no_fee=%.4f, net=%.4f",
            spread, n, yes_fee, no_fee, guaranteed_profit,
        )
        return
    _log.info(
        "PURE ARB: ticker=%s, yes_ask=%.2f, no_ask=%.2f, spread=%.4f, n=%d, guaranteed_profit=%.2f",
        ticker, yes_ask, no_ask, spread, n, guaranteed_profit,
    )

    await executor.place_arb_pair(
        ticker=ticker,
        yes_ask=yes_ask,
        no_ask=no_ask,
        n_contracts=n,
        client=client,
        session=session,
    )


# ── Phase 2: Market scanning ───────────────────────────────────────────────────

async def _evaluate_market(
    market: dict,
    client: KalshiClient,
    session: aiohttp.ClientSession,
    balance: float,
) -> Optional[dict]:
    ticker = market.get("ticker", "")
    title = market.get("title", "")

    close_time_str = market.get("close_time", "")
    close_dt: Optional[datetime] = None
    hours_to_close: Optional[float] = None
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        hours_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except (ValueError, AttributeError):
        pass

    ob = await client.get_orderbook(ticker)
    if ob is None:
        return None

    # ── Pure arbitrage detection ──────────────────────────────────────────────
    # If YES_ask + NO_ask < 1.0, buying both sides guarantees riskless profit.
    # Check BEFORE fair value fetch — arb doesn't need external source data.
    if ob.yes_ask > 0 and ob.no_ask > 0 and ob.yes_ask + ob.no_ask < 1.0:
        arb_spread = 1.0 - ob.yes_ask - ob.no_ask
        _log.info(
            "ARB DETECTED on %s: yes_ask=%.2f + no_ask=%.2f = %.2f < 1.0, spread=%.4f",
            ticker, ob.yes_ask, ob.no_ask, ob.yes_ask + ob.no_ask, arb_spread,
        )
        await _place_arb_trade(ticker, ob.yes_ask, ob.no_ask, balance, client, session)
        return None  # Skip normal EV analysis

    # Spread filter
    spread = ob.yes_ask - ob.yes_bid
    if spread > config.MAX_BID_ASK_SPREAD:
        return None

    # Determine category BEFORE fair value fetch so weights are category-aware
    category = risk_manager.get_position_category(title)
    corr_mult, _ = risk_manager.get_correlation_stake_multiplier(title)
    if corr_mult == 0.0:
        _log.debug("Pre-flight correlation block: %s (category=%s)", ticker, category)
        return None

    fv = await fair_value.get_fair_value(
        kalshi_ticker=ticker,
        kalshi_title=title,
        kalshi_close_date=close_dt,
        session=session,
        category=category,  # Enables category-specific source weights
    )
    if fv is None:
        return None

    if ob.yes_ask <= 0 or ob.no_ask <= 0:
        return None

    try:
        result = edge_calculator.compute_edge(
            w=fv.probability,
            p=ob.yes_ask,
            q=ob.no_ask,
            balance=balance,
            ticker=ticker,
            yes_bid=ob.yes_bid,
            no_bid=ob.no_bid,
            hours_to_close=hours_to_close,
            source_count=fv.source_count,
            source_disagreement_mult=fv.source_disagreement_mult,
        )
    except ValueError:
        return None

    if result.direction == "NONE" or result.net_edge < result.min_edge_used:
        return None

    return {
        "ticker": ticker,
        "title": title,
        "direction": result.direction,
        "price_cents": int(result.exec_price * 100),
        "mid_price_cents": int(result.market_price * 100),
        "contracts": result.contracts,       # Pre-computed with fee deducted
        "stake_usd": result.stake_usd,       # Actual cost = contracts*price + fee
        "eval_spread": ob.yes_ask - ob.yes_bid,  # Spread at eval time (for momentum filter)
        "net_edge": result.net_edge,
        "gross_edge": result.gross_edge,
        "kelly_fraction": result.kelly_fraction,
        "adjusted_kelly": result.adjusted_kelly,
        "fee_usd": result.fee_usd,
        "fair_prob": fv.probability,
        "fair_prob_sources": fv.sources,
        "source_count": fv.source_count,
        "uncertainty_mult": result.uncertainty_mult,
        "time_decay_mult": result.time_decay_mult,
        "category": category,
        "ob_yes_ask": ob.yes_ask,
        "ob_no_ask": ob.no_ask,
        "close_dt": close_dt,
    }


async def _scan_markets(client: KalshiClient, session: aiohttp.ClientSession) -> None:
    tradeable, reason = risk_manager.can_trade()
    if not tradeable:
        _log.info("Skipping market scan — %s", reason)
        return

    balance = await client.get_balance()
    if balance is None:
        balance = dashboard._current_balance
    else:
        dashboard.update_balance(balance)

    if balance <= 0:
        _log.warning("Zero or negative balance — skipping scan.")
        return

    _log.info("Starting market scan. Balance: $%.2f", balance)
    all_markets = await client.get_all_open_markets()

    now = datetime.now(timezone.utc)
    min_close = now + timedelta(hours=config.MIN_TIME_TO_CLOSE_HR)
    max_close = now + timedelta(days=config.MAX_TIME_TO_CLOSE_DAYS)
    open_tickers_set = set(state_manager.open_tickers())

    candidates_input = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        if ticker in open_tickers_set:
            continue
        if (m.get("volume", 0) or 0) < config.MIN_MARKET_VOLUME:
            continue
        market_type = m.get("market_type", "")
        if market_type and market_type.lower() not in ("binary", ""):
            continue
        close_time_str = m.get("close_time", "")
        try:
            close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            if close_dt < min_close or close_dt > max_close:
                continue
        except (ValueError, AttributeError):
            pass
        candidates_input.append(m)

    _log.info("Evaluating %d candidate markets after filters...", len(candidates_input))

    semaphore = asyncio.Semaphore(config.CONCURRENT_MARKET_SCANS)

    async def _evaluate_with_semaphore(market: dict) -> Optional[dict]:
        async with semaphore:
            try:
                return await _evaluate_market(market, client, session, balance)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, OSError) as exc:
                _log.debug("Evaluation failed for %s: %s", market.get("ticker"), exc)
                return None

    results = await asyncio.gather(*[_evaluate_with_semaphore(m) for m in candidates_input])
    edges = [r for r in results if r is not None]

    if not edges:
        _log.info("No edge found across %d markets.", len(candidates_input))
        return

    edges.sort(key=lambda x: x["net_edge"], reverse=True)
    _log.info(
        "Found %d markets with edge. Top: %s %.1f%% (sources=%d, decay=%.0f%%, uncertainty=%.0f%%)",
        len(edges), edges[0]["ticker"], edges[0]["net_edge"] * 100,
        edges[0]["source_count"], edges[0]["time_decay_mult"] * 100,
        edges[0]["uncertainty_mult"] * 100,
    )

    for candidate in edges:
        tradeable, reason = risk_manager.can_trade(candidate["title"])
        if not tradeable:
            _log.info("Trading stopped mid-scan: %s", reason)
            break

        ticker = candidate["ticker"]
        ob = await client.get_orderbook(ticker)
        if ob is None:
            continue

        direction = candidate["direction"]
        original_price = candidate["price_cents"]
        fresh_price = int((ob.yes_ask if direction == "YES" else ob.no_ask) * 100)

        if abs(fresh_price - original_price) > config.PRICE_STALENESS_CENTS:
            _log.info(
                "Stale price on %s: was %dc, now %dc. Skipping.",
                ticker, original_price, fresh_price,
            )
            continue

        # Spread momentum filter: skip if spread widened significantly
        # A rapidly widening spread signals liquidity withdrawal
        fresh_spread = ob.yes_ask - ob.yes_bid
        eval_spread = candidate.get("eval_spread", 0.0)
        if eval_spread > 0 and fresh_spread > eval_spread * config.SPREAD_WIDENING_FACTOR:
            _log.info(
                "Spread widening on %s: eval=%.3f, now=%.3f (%.0f%% wider). Skipping.",
                ticker, eval_spread, fresh_spread,
                (fresh_spread / eval_spread - 1) * 100,
            )
            continue

        fresh_mid = int(
            edge_calculator._compute_midpoint(
                ob.yes_bid if direction == "YES" else ob.no_bid,
                ob.yes_ask if direction == "YES" else ob.no_ask,
            ) * 100
        )

        await executor.place_bet(
            ticker=ticker,
            market_title=candidate["title"],
            direction=direction,
            contracts=candidate["contracts"],       # Pre-computed, fee-aware
            stake_usd=candidate["stake_usd"],
            price_cents=fresh_price,
            mid_price_cents=fresh_mid,
            fair_prob=candidate["fair_prob"],
            gross_edge=candidate["gross_edge"],
            net_edge=candidate["net_edge"],
            kelly_fraction=candidate["kelly_fraction"],
            adjusted_kelly=candidate["adjusted_kelly"],
            fee_usd=candidate["fee_usd"],
            fair_prob_sources=candidate["fair_prob_sources"],
            source_count=candidate["source_count"],
            uncertainty_mult=candidate["uncertainty_mult"],
            time_decay_mult=candidate["time_decay_mult"],
            category=candidate["category"],
            client=client,
            session=session,
        )
        await asyncio.sleep(0.5)


# ── Daily summary ──────────────────────────────────────────────────────────────

async def _daily_summary_loop(session: aiohttp.ClientSession) -> None:
    while True:
        now = datetime.now(timezone.utc)
        if (now.hour == config.DAILY_SUMMARY_UTC_HOUR
                and now.minute == config.DAILY_SUMMARY_UTC_MINUTE):
            await _send_daily_summary(session)
            await asyncio.sleep(90)
        else:
            await asyncio.sleep(30)


# ── Main loop ──────────────────────────────────────────────────────────────────

async def main_loop(single_cycle: bool = False) -> None:
    load_dotenv()
    _validate_env()
    config.validate_config()

    os.makedirs(config.LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)

    # SIGTERM handler: graceful shutdown (same as KeyboardInterrupt)
    def _handle_sigterm(signum, frame):
        _log.info("Received SIGTERM — initiating graceful shutdown.")
        state_manager.save()
        bot_logger.log_event("shutdown", "Bot shut down via SIGTERM.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    state_manager.load()
    market_matcher.initialise()

    async with aiohttp.ClientSession() as session:
        client = KalshiClient(session)
        startup_results = await _startup_checks(session, client)
        balance = dashboard._current_balance

        if state_manager.needs_daily_reset():
            risk_manager.reset_daily(balance)

        bot_logger.log_event(
            "startup",
            f"Bot started in {'PAPER' if config.PAPER_MODE else 'LIVE'} mode. Balance: ${balance:.2f}",
            extra={
                "paper_mode": config.PAPER_MODE,
                "balance_usd": balance,
                "connectivity": startup_results,
                "weights": {
                    "predictit": config.PREDICTIT_WEIGHT,
                    "manifold": config.MANIFOLD_WEIGHT,
                    "polymarket": config.POLYMARKET_WEIGHT,
                },
            },
        )
        await _send_startup_alert(session, balance)

        console.print(
            f"\n[bold bright_blue]⬡ KALSHI EV BOT[/bold bright_blue] "
            f"[{'bold yellow' if config.PAPER_MODE else 'bold green'}]"
            f"{'◈ PAPER MODE' if config.PAPER_MODE else '◉ LIVE TRADING'}[/]\n"
            f"Balance: [bold]${balance:.2f}[/bold]  |  "
            f"Kalshi: {'[green]OK[/]' if startup_results['kalshi'] else '[red]FAIL[/]'}  |  "
            f"Manifold: {'[green]OK[/]' if startup_results.get('manifold') else '[red]FAIL[/]'}  |  "
            f"PredictIt: {'[green]OK[/]' if startup_results.get('predictit') else '[red]FAIL[/]'}  |  "
            f"Polymarket: {'[green]OK[/]' if startup_results.get('polymarket') else '[red]FAIL[/]'}\n"
            f"[dim]Weights: PredictIt={config.PREDICTIT_WEIGHT:.0%} | "
            f"Manifold={config.MANIFOLD_WEIGHT:.0%} | "
            f"Polymarket={config.POLYMARKET_WEIGHT:.0%}[/dim]\n"
        )

        if single_cycle:
            console.print("[bold]Running single scan cycle...[/bold]")
            await _manage_positions(client, session)
            await _scan_markets(client, session)
            dashboard.print_snapshot()
            return

        dashboard_task = asyncio.create_task(dashboard.run_dashboard())
        daily_task = asyncio.create_task(_daily_summary_loop(session))

        try:
            while True:
                loop_start = time.monotonic()
                await _manage_positions(client, session)
                await _scan_markets(client, session)
                next_scan = time.monotonic() + config.SCAN_INTERVAL_SEC
                dashboard.update_next_scan(next_scan)
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0, config.SCAN_INTERVAL_SEC - elapsed)
                _log.info("Scan complete in %.1fs. Next in %.0fs.", elapsed, sleep_time)
                await asyncio.sleep(sleep_time)
        except (KeyboardInterrupt, asyncio.CancelledError):
            dashboard_task.cancel()
            daily_task.cancel()
            state_manager.save()  # Flush any pending state (daily PnL, etc.)
            bot_logger.log_event("shutdown", "Bot shut down gracefully.")
            console.print("\n[bold red]Bot stopped.[/bold red]")


def main() -> None:
    asyncio.run(main_loop(single_cycle=False))


def run_single_cycle() -> None:
    asyncio.run(main_loop(single_cycle=True))


if __name__ == "__main__":
    import sys
    asyncio.run(main_loop(single_cycle="--single" in sys.argv))
