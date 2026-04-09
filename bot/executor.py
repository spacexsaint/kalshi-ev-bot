"""
executor.py — Order placement, fill tracking, Discord alerts.

AUDIT IMPROVEMENTS (2026-04-08):
  - exec_price (ask) vs market_price (midpoint) now tracked separately
  - Correlation stake multiplier applied from risk_manager
  - state_manager.add_position() now receives full metadata
  - Discord alerts include source_count, uncertainty_mult, time_decay_mult
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


# ── Fill result ────────────────────────────────────────────────────────────────

@dataclass
class FillResult:
    success: bool
    filled_contracts: float
    partial: bool
    order_id: str
    client_order_id: str


async def _simulate_paper_fill(num_contracts: int, client_order_id: str) -> FillResult:
    await asyncio.sleep(0.05)
    return FillResult(
        success=True,
        filled_contracts=float(num_contracts),
        partial=False,
        order_id=f"PAPER-{client_order_id[:8]}",
        client_order_id=client_order_id,
    )


async def _execute_live_order(
    client: KalshiClient,
    ticker: str,
    side: str,
    price_cents: int,
    num_contracts: int,
    client_order_id: str,
) -> FillResult:
    order = await client.place_order(
        ticker=ticker,
        side=side,
        price_cents=price_cents,
        num_contracts=num_contracts,
        client_order_id=client_order_id,
    )
    if order is None:
        return FillResult(False, 0.0, False, "", client_order_id)

    order_id = order.get("order_id", "")
    _log.info("Order placed: %s (id=%s, contracts=%d @ %dc)", ticker, order_id, num_contracts, price_cents)

    poll_interval = 3.0
    elapsed = 0.0
    filled_contracts = 0.0

    while elapsed < config.ORDER_FILL_TIMEOUT_S:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        status = await client.get_order_status(order_id)
        if status is None:
            continue

        order_status = status.get("status", "")
        try:
            filled_contracts = float(status.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            filled_contracts = 0.0

        if order_status == "filled":
            return FillResult(True, filled_contracts, False, order_id, client_order_id)
        if order_status in ("canceled", "cancelled"):
            return FillResult(
                filled_contracts > 0, filled_contracts, filled_contracts > 0,
                order_id, client_order_id,
            )

    # Timeout — cancel remainder
    _log.warning("Order %s timed out. Cancelling.", order_id)
    await client.cancel_order(order_id)
    final = await client.get_order_status(order_id)
    if final:
        try:
            filled_contracts = float(final.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            pass

    if filled_contracts == 0:
        return FillResult(False, 0.0, False, order_id, client_order_id)
    return FillResult(True, filled_contracts, True, order_id, client_order_id)


# ── Public entry-point ─────────────────────────────────────────────────────────

async def place_bet(
    *,
    ticker: str,
    market_title: str,
    direction: str,
    stake_usd: float,
    price_cents: int,          # Execution price (ask)
    mid_price_cents: int,      # Midpoint (for tracking)
    fair_prob: float,
    gross_edge: float,
    net_edge: float,
    kelly_fraction: float,
    adjusted_kelly: float,
    fee_usd: float,
    fair_prob_sources: List[str],
    source_count: int,
    uncertainty_mult: float,
    time_decay_mult: float,
    category: str,
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> bool:
    price_decimal = price_cents / 100.0
    side = direction.lower()

    # Apply correlation stake penalty
    corr_mult, _ = risk_manager.get_correlation_stake_multiplier(market_title)
    if corr_mult == 0.0:
        _log.info("Correlation block on %s (category=%s)", ticker, category)
        return False

    effective_stake = stake_usd * corr_mult
    num_contracts = math.floor(effective_stake / price_decimal)

    if num_contracts < 1:
        _log.info(
            "Stake too small for %s: $%.2f at %dc → 0 contracts. Skipping.",
            ticker, effective_stake, price_cents,
        )
        return False

    client_order_id = str(uuid.uuid4())

    _log.info(
        "[%s] %s %s: %d contracts @ %dc (stake=$%.2f, mid=%dc, edge=%.1f%%, "
        "uncertainty=%.0f%%, decay=%.0f%%, sources=%d)",
        "PAPER" if config.PAPER_MODE else "LIVE",
        direction, ticker, num_contracts, price_cents,
        effective_stake, mid_price_cents,
        net_edge * 100, uncertainty_mult * 100, time_decay_mult * 100,
        source_count,
    )

    if config.PAPER_MODE:
        fill = await _simulate_paper_fill(num_contracts, client_order_id)
    else:
        fill = await _execute_live_order(
            client, ticker, side, price_cents, num_contracts, client_order_id
        )

    if not fill.success or fill.filled_contracts == 0:
        bot_logger.log_trade(
            ticker=ticker,
            market_title=market_title,
            direction=direction,
            entry_price_cents=price_cents,
            contracts=float(num_contracts),
            stake_usd=effective_stake,
            fair_prob=fair_prob,
            fair_prob_sources=fair_prob_sources,
            gross_edge=gross_edge,
            net_edge=net_edge,
            fee_usd=fee_usd,
            kelly_fraction=adjusted_kelly,
            filled=False,
            filled_contracts=0.0,
            paper_mode=config.PAPER_MODE,
        )
        return False

    actual_stake = fill.filled_contracts * price_decimal

    # Store position with full metadata
    state_manager.add_position(
        ticker=ticker,
        direction=direction,
        entry_price_cents=price_cents,
        contracts=fill.filled_contracts,
        stake_usd=actual_stake,
        fair_prob_at_entry=fair_prob,
        net_edge_at_entry=net_edge,
        client_order_id=client_order_id,
        market_title=market_title,
        category=category,
        exec_price_cents=price_cents,
        mid_price_cents=mid_price_cents,
        gross_edge_at_entry=gross_edge,
        source_count=source_count,
        sources=fair_prob_sources,
        uncertainty_mult=uncertainty_mult,
        time_decay_mult=time_decay_mult,
    )
    risk_manager.record_fill(actual_stake)

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
        kelly_fraction=adjusted_kelly,
        filled=True,
        filled_contracts=fill.filled_contracts,
        paper_mode=config.PAPER_MODE,
    )

    mode = _fmt_mode()
    partial_note = " *(partial fill)*" if fill.partial else ""
    corr_note = f" *(correlation penalty: {corr_mult:.0%})*" if corr_mult < 1.0 else ""
    alert = (
        f"🎯 **BET PLACED** {mode}{partial_note}{corr_note}\n"
        f"Market: {market_title}\n"
        f"Direction: **{direction}** at **{price_cents}¢** (mid: {mid_price_cents}¢)\n"
        f"Stake: **${actual_stake:.2f}** | Contracts: **{fill.filled_contracts:.1f}**\n"
        f"Fair Prob: **{fair_prob:.1%}** | Net Edge: **{net_edge:.1%}** | "
        f"Gross: **{gross_edge:.1%}**\n"
        f"Sources: {', '.join(fair_prob_sources)} ({source_count} matched)\n"
        f"Uncertainty: {uncertainty_mult:.0%} | Decay: {time_decay_mult:.0%} | "
        f"Category: {category}"
    )
    await _send_discord(session, alert)
    return True


async def close_position(
    *,
    position: dict,
    current_bid_cents: int,
    reason: str,
    resolution_pnl: Optional[float] = None,
    client: KalshiClient,
    session: aiohttp.ClientSession,
) -> None:
    ticker = position["ticker"]
    direction = position["direction"]
    entry_cents = position["entry_price_cents"]
    contracts = position["contracts"]
    opened_at = position["opened_at"]
    client_order_id = position["client_order_id"]

    if resolution_pnl is not None:
        pnl_usd = resolution_pnl
    else:
        exit_decimal = current_bid_cents / 100.0
        entry_decimal = entry_cents / 100.0
        pnl_usd = (exit_decimal - entry_decimal) * contracts

    try:
        opened_dt = datetime.fromisoformat(opened_at)
        held_s = (datetime.now(timezone.utc) - opened_dt).total_seconds()
    except (ValueError, TypeError):
        held_s = 0.0

    if not config.PAPER_MODE and reason != "resolved":
        import math
        sell_side = "yes" if direction == "YES" else "no"
        sell_contracts = math.floor(contracts)
        if sell_contracts >= 1:
            await _execute_live_order(
                client=client,
                ticker=ticker,
                side=sell_side,
                price_cents=current_bid_cents,
                num_contracts=sell_contracts,
                client_order_id=str(uuid.uuid4()),
            )

    state_manager.remove_position(client_order_id)
    risk_manager.record_pnl(pnl_usd)

    bot_logger.log_position_closed(
        ticker=ticker,
        market_title=position.get("market_title", ticker),
        direction=direction,
        entry_price_cents=entry_cents,
        exit_price_cents=current_bid_cents,
        contracts=contracts,
        pnl_usd=pnl_usd,
        held_seconds=held_s,
        paper_mode=config.PAPER_MODE,
    )

    mode = _fmt_mode()
    pnl_sign = "+" if pnl_usd >= 0 else ""
    held_str = f"{held_s / 3600:.1f}h" if held_s >= 3600 else f"{held_s / 60:.0f}m"
    alert = (
        f"{'✅' if pnl_usd >= 0 else '🔴'} **CLOSED** {mode} — "
        f"P&L: **{pnl_sign}${pnl_usd:.2f}**\n"
        f"Market: {ticker} | Direction: {direction} | "
        f"Held: {held_str} | Reason: {reason}\n"
        f"Entry: {entry_cents}¢ → Exit: {current_bid_cents}¢ | "
        f"Contracts: {contracts:.1f}"
    )
    await _send_discord(session, alert)
