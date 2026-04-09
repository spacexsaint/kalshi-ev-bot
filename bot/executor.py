"""
executor.py — Order placement, fill tracking, retry logic, Discord alerts.

place_bet() workflow:
  1. Convert stake_usd to num_contracts (stake_usd / price_decimal)
  2. Round DOWN to whole contracts
  3. If num_contracts < 1: log "stake too small" and return
  4. Generate unique client_order_id (UUID4)
  5. Call kalshi_client.place_order(...)
  6. Poll get_order_status every 3 seconds for up to ORDER_FILL_TIMEOUT_S
  7. Track partial fills: record filled_contracts each poll
  8. If timeout with partial fill: cancel remainder, record partial position
  9. If timeout with zero fill: cancel, log, return
 10. On any fill: call state_manager.add_position + risk_manager.record_fill + Discord alert

PAPER MODE: orders are not sent to the exchange — we simulate fills immediately.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp

from bot import config
from bot import logger as bot_logger
from bot import risk_manager
from bot import state_manager
from bot.kalshi_client import KalshiClient

_log = logging.getLogger(__name__)


# ── Discord alerting ───────────────────────────────────────────────────────────

async def _send_discord(session: aiohttp.ClientSession, content: str) -> None:
    """Post a message to the Discord webhook. Silently skip if URL not configured."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        async with session.post(
            webhook_url,
            json={"content": content},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 204):
                _log.warning("Discord webhook returned HTTP %d", resp.status)
    except Exception as exc:
        _log.warning("Discord alert failed: %s", exc)


def _fmt_mode() -> str:
    return "`PAPER`" if config.PAPER_MODE else "`LIVE`"


# ── Paper-mode simulation ──────────────────────────────────────────────────────

@dataclass
class FillResult:
    success: bool
    filled_contracts: float
    partial: bool        # True if fewer than requested filled
    order_id: str
    client_order_id: str


async def _simulate_paper_fill(num_contracts: int, client_order_id: str) -> FillResult:
    """Simulate an immediate full fill in paper mode."""
    await asyncio.sleep(0.05)   # tiny async yield
    return FillResult(
        success=True,
        filled_contracts=float(num_contracts),
        partial=False,
        order_id=f"PAPER-{client_order_id[:8]}",
        client_order_id=client_order_id,
    )


# ── Live order execution ───────────────────────────────────────────────────────

async def _execute_live_order(
    client: KalshiClient,
    ticker: str,
    side: str,
    price_cents: int,
    num_contracts: int,
    client_order_id: str,
) -> FillResult:
    """
    Place a live limit order and poll for fills.

    Polls every 3 seconds for ORDER_FILL_TIMEOUT_S seconds.
    Cancels remainder on timeout. Returns actual filled quantity.
    """
    # Place order
    order = await client.place_order(
        ticker=ticker,
        side=side,
        price_cents=price_cents,
        num_contracts=num_contracts,
        client_order_id=client_order_id,
    )
    if order is None:
        _log.error("Order placement failed for %s", ticker)
        return FillResult(
            success=False,
            filled_contracts=0.0,
            partial=False,
            order_id="",
            client_order_id=client_order_id,
        )

    order_id = order.get("order_id", "")
    _log.info("Order placed: %s (id=%s, contracts=%d @ %dc)", ticker, order_id, num_contracts, price_cents)

    # Poll for fills
    poll_interval = 3.0
    elapsed = 0.0
    filled_contracts = 0.0

    while elapsed < config.ORDER_FILL_TIMEOUT_S:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        status = await client.get_order_status(order_id)
        if status is None:
            _log.warning("Could not get status for order %s", order_id)
            continue

        order_status = status.get("status", "")
        try:
            filled_contracts = float(status.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            filled_contracts = 0.0

        _log.debug(
            "Order %s status=%s filled=%.1f elapsed=%.0fs",
            order_id, order_status, filled_contracts, elapsed,
        )

        if order_status == "filled":
            return FillResult(
                success=True,
                filled_contracts=filled_contracts,
                partial=False,
                order_id=order_id,
                client_order_id=client_order_id,
            )

        if order_status in ("canceled", "cancelled"):
            return FillResult(
                success=filled_contracts > 0,
                filled_contracts=filled_contracts,
                partial=filled_contracts > 0,
                order_id=order_id,
                client_order_id=client_order_id,
            )

    # Timeout reached — cancel remainder
    _log.warning("Order %s timed out after %ds. Cancelling remainder.", order_id, config.ORDER_FILL_TIMEOUT_S)
    await client.cancel_order(order_id)

    # Re-fetch final state
    final_status = await client.get_order_status(order_id)
    if final_status:
        try:
            filled_contracts = float(final_status.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            pass

    if filled_contracts == 0:
        _log.info("Order %s: zero fill after timeout. No position taken.", order_id)
        return FillResult(
            success=False,
            filled_contracts=0.0,
            partial=False,
            order_id=order_id,
            client_order_id=client_order_id,
        )

    _log.info("Order %s: partial fill of %.1f contracts.", order_id, filled_contracts)
    return FillResult(
        success=True,
        filled_contracts=filled_contracts,
        partial=True,
        order_id=order_id,
        client_order_id=client_order_id,
    )


# ── Public entry-point ─────────────────────────────────────────────────────────

async def place_bet(
    *,
    ticker: str,
    market_title: str,
    direction: str,        # "YES" | "NO"
    stake_usd: float,
    price_cents: int,      # e.g. 55 for 55 cents
    fair_prob: float,
    gross_edge: float,
    net_edge: float,
    kelly_fraction: float,
    fee_usd: float,
    fair_prob_sources: List[str],
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> bool:
    """
    Execute a bet. Handles paper mode, sizing, fill tracking, state & alerts.

    Returns:
        True if any contracts were filled, False otherwise.
    """
    price_decimal = price_cents / 100.0
    side = direction.lower()

    # Step 1: Convert stake → contracts
    num_contracts = math.floor(stake_usd / price_decimal)

    # Step 2: Check minimum
    if num_contracts < 1:
        _log.info(
            "Stake too small for %s: $%.2f at %dc → 0 contracts. Skipping.",
            ticker, stake_usd, price_cents,
        )
        bot_logger.log_event(
            "skip_too_small",
            f"Stake ${stake_usd:.2f} too small for {ticker} at {price_cents}c",
            extra={"ticker": ticker, "stake_usd": stake_usd, "price_cents": price_cents},
        )
        return False

    # Step 3: Unique client order ID
    client_order_id = str(uuid.uuid4())

    _log.info(
        "[%s] Placing %s %s: %d contracts @ %dc (stake=$%.2f, edge=%.1f%%)",
        "PAPER" if config.PAPER_MODE else "LIVE",
        direction, ticker, num_contracts, price_cents, stake_usd, net_edge * 100,
    )

    # Step 4: Execute (paper or live)
    if config.PAPER_MODE:
        fill = await _simulate_paper_fill(num_contracts, client_order_id)
    else:
        fill = await _execute_live_order(
            client, ticker, side, price_cents, num_contracts, client_order_id
        )

    # Step 5: Handle result
    if not fill.success or fill.filled_contracts == 0:
        bot_logger.log_trade(
            ticker=ticker,
            market_title=market_title,
            direction=direction,
            entry_price_cents=price_cents,
            contracts=float(num_contracts),
            stake_usd=stake_usd,
            fair_prob=fair_prob,
            fair_prob_sources=fair_prob_sources,
            gross_edge=gross_edge,
            net_edge=net_edge,
            fee_usd=fee_usd,
            kelly_fraction=kelly_fraction,
            filled=False,
            filled_contracts=0.0,
            paper_mode=config.PAPER_MODE,
        )
        return False

    # Step 6: Record position
    actual_stake = fill.filled_contracts * price_decimal
    state_manager.add_position(
        ticker=ticker,
        direction=direction,
        entry_price_cents=price_cents,
        contracts=fill.filled_contracts,
        stake_usd=actual_stake,
        fair_prob_at_entry=fair_prob,
        net_edge_at_entry=net_edge,
        client_order_id=client_order_id,
    )
    risk_manager.record_fill(actual_stake)

    # Step 7: Log trade
    bot_logger.log_trade(
        ticker=ticker,
        market_title=market_title,
        direction=direction,
        entry_price_cents=price_cents,
        contracts=float(num_contracts),
        stake_usd=actual_stake,
        fair_prob=fair_prob,
        fair_prob_sources=fair_prob_sources,
        gross_edge=gross_edge,
        net_edge=net_edge,
        fee_usd=fee_usd,
        kelly_fraction=kelly_fraction,
        filled=True,
        filled_contracts=fill.filled_contracts,
        paper_mode=config.PAPER_MODE,
    )

    # Step 8: Discord alert
    mode = _fmt_mode()
    partial_note = " *(partial fill)*" if fill.partial else ""
    alert = (
        f"🎯 **BET PLACED** {mode}{partial_note}\n"
        f"Market: {market_title}\n"
        f"Direction: **{direction}** at **{price_cents}¢**\n"
        f"Stake: **${actual_stake:.2f}** | Contracts: **{fill.filled_contracts:.1f}**\n"
        f"Fair Prob: **{fair_prob:.1%}** | Net Edge: **{net_edge:.1%}**\n"
        f"Sources: {', '.join(fair_prob_sources)}"
    )
    await _send_discord(session, alert)

    _log.info(
        "Position opened: %s %s, %.1f contracts @ %dc, edge=%.1f%%",
        direction, ticker, fill.filled_contracts, price_cents, net_edge * 100,
    )
    return True


async def close_position(
    *,
    position: dict,
    current_bid_cents: int,   # Best bid for our direction (to sell into)
    reason: str,              # "profit_take" | "resolved" | "manual"
    resolution_pnl: Optional[float] = None,   # For resolved markets
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> None:
    """
    Close an open position.

    For paper mode: simulates immediate close.
    For live mode: places a market sell (limit at bid).
    """
    ticker = position["ticker"]
    direction = position["direction"]
    entry_cents = position["entry_price_cents"]
    contracts = position["contracts"]
    opened_at = position["opened_at"]
    client_order_id = position["client_order_id"]

    # Compute PnL
    if resolution_pnl is not None:
        pnl_usd = resolution_pnl
    else:
        # Closing into market: pnl = (exit_price - entry_price) * contracts
        exit_decimal = current_bid_cents / 100.0
        entry_decimal = entry_cents / 100.0
        pnl_usd = (exit_decimal - entry_decimal) * contracts

    # Calculate hold duration
    try:
        opened_dt = datetime.fromisoformat(opened_at)
        held_s = (datetime.now(timezone.utc) - opened_dt).total_seconds()
    except (ValueError, TypeError):
        held_s = 0.0

    # Place sell order (live only)
    if not config.PAPER_MODE and reason != "resolved":
        sell_side = "yes" if direction == "YES" else "no"
        sell_contracts = math.floor(contracts)
        if sell_contracts >= 1:
            sell_coid = str(uuid.uuid4())
            await _execute_live_order(
                client=client,
                ticker=ticker,
                side=sell_side,
                price_cents=current_bid_cents,
                num_contracts=sell_contracts,
                client_order_id=sell_coid,
            )

    # Update state
    state_manager.remove_position(client_order_id)
    risk_manager.record_pnl(pnl_usd)

    # Log
    bot_logger.log_position_closed(
        ticker=ticker,
        market_title=ticker,
        direction=direction,
        entry_price_cents=entry_cents,
        exit_price_cents=current_bid_cents,
        contracts=contracts,
        pnl_usd=pnl_usd,
        held_seconds=held_s,
        paper_mode=config.PAPER_MODE,
    )

    # Discord
    mode = _fmt_mode()
    pnl_sign = "+" if pnl_usd >= 0 else ""
    held_str = f"{held_s / 3600:.1f}h" if held_s >= 3600 else f"{held_s / 60:.0f}m"
    alert = (
        f"{'✅' if pnl_usd >= 0 else '🔴'} **CLOSED** {mode} — P&L: **{pnl_sign}${pnl_usd:.2f}**\n"
        f"Market: {ticker} | Direction: {direction} | Held: {held_str}\n"
        f"Entry: {entry_cents}¢ → Exit: {current_bid_cents}¢ | Contracts: {contracts:.1f}"
    )
    await _send_discord(session, alert)

    _log.info(
        "Position closed: %s %s, PnL=%+.2f, held=%s",
        direction, ticker, pnl_usd, held_str,
    )
