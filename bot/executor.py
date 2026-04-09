"""
executor.py — Order placement, fill tracking, retry logic, Discord alerts.

IMPORTANT: Edge calculator now pre-computes the exact contract count with
fee already deducted (no overrun). Executor uses result.contracts directly
instead of re-deriving from stake/price (which ignores fee).

Paper mode: simulates immediate full fill, no API calls.
Live mode: places limit order at ask, polls for fills, cancels on timeout.
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


# ── Discord ────────────────────────────────────────────────────────────────────
async def _send_discord(session: aiohttp.ClientSession, content: str) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        async with session.post(
            webhook_url, json={"content": content},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 204):
                _log.warning("Discord webhook HTTP %d", resp.status)
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
    # Actual fees paid as reported by Kalshi API (taker + maker, both in dollars)
    # If None, we fall back to computed taker fee estimate
    actual_taker_fee: Optional[float] = None
    actual_maker_fee: Optional[float] = None

    @property
    def actual_total_fee(self) -> Optional[float]:
        """Total actual fee if available from API response."""
        if self.actual_taker_fee is not None or self.actual_maker_fee is not None:
            return (self.actual_taker_fee or 0.0) + (self.actual_maker_fee or 0.0)
        return None


async def _simulate_paper_fill(num_contracts: int, client_order_id: str) -> FillResult:
    await asyncio.sleep(0.05)
    return FillResult(True, float(num_contracts), False, f"PAPER-{client_order_id[:8]}", client_order_id)


async def _execute_live_order(
    client: KalshiClient,
    ticker: str,
    side: str,
    price_cents: int,
    num_contracts: int,
    client_order_id: str,
) -> FillResult:
    order = await client.place_order(ticker, side, price_cents, num_contracts, client_order_id)
    if order is None:
        return FillResult(False, 0.0, False, "", client_order_id)

    order_id = order.get("order_id", "")
    _log.info("Order placed: %s id=%s contracts=%d @ %dc", ticker, order_id, num_contracts, price_cents)

    elapsed = 0.0
    filled_contracts = 0.0
    poll_s = 3.0

    while elapsed < config.ORDER_FILL_TIMEOUT_S:
        await asyncio.sleep(poll_s)
        elapsed += poll_s
        status = await client.get_order_status(order_id)
        if status is None:
            continue
        s = status.get("status", "")
        try:
            filled_contracts = float(status.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            pass
        if s == "filled":
            return FillResult(True, filled_contracts, False, order_id, client_order_id)
        if s in ("canceled", "cancelled"):
            return FillResult(filled_contracts > 0, filled_contracts, filled_contracts > 0, order_id, client_order_id)

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


def _extract_fees_from_order(order_status: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """
    Extract actual taker and maker fees paid from Kalshi order status response.

    Kalshi returns fees in the order object as:
      taker_fees_dollars: str (e.g. "0.1800")
      maker_fees_dollars: str (e.g. "0.0000")

    When a limit order rests on the book and is filled as a maker, taker_fees_dollars
    will be 0.00 and maker_fees_dollars will reflect the actual (4x cheaper) fee.
    Tracking this lets us measure the true cost of each trade and validate the
    fee model against reality.
    """
    if not order_status:
        return None, None
    try:
        taker = float(order_status.get("taker_fees_dollars", "") or 0)
        maker = float(order_status.get("maker_fees_dollars", "") or 0)
        return (taker if taker > 0 else None), (maker if maker > 0 else None)
    except (TypeError, ValueError):
        return None, None


# ── place_bet ──────────────────────────────────────────────────────────────────
async def place_bet(
    *,
    ticker: str,
    market_title: str,
    direction: str,
    contracts: int,              # Pre-computed by edge_calculator (fee-aware)
    stake_usd: float,            # Actual total cost = contracts*price + fee
    price_cents: int,
    mid_price_cents: int,
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
    """
    Execute a bet. Uses pre-computed contracts from edge_calculator (fee-aware).
    Applies correlation penalty to contracts (reduces count, not re-deriving from stake).
    """
    side = direction.lower()

    # Correlation penalty — reduce contracts proportionally
    corr_mult, _ = risk_manager.get_correlation_stake_multiplier(market_title)
    if corr_mult == 0.0:
        _log.info("Correlation block: %s (category=%s)", ticker, category)
        return False

    # Apply correlation penalty to contract count
    effective_contracts = math.floor(contracts * corr_mult)
    if effective_contracts < 1:
        _log.info("Post-correlation contracts < 1 for %s. Skipping.", ticker)
        return False

    # Recalculate actual stake after correlation adjustment
    price_decimal = price_cents / 100.0
    from bot.fee_calculator import compute_taker_fee
    actual_fee = compute_taker_fee(price_decimal, effective_contracts, ticker)
    actual_stake = effective_contracts * price_decimal + actual_fee

    client_order_id = str(uuid.uuid4())

    _log.info(
        "[%s] %s %s: %d contracts @ %dc "
        "(cost=$%.2f fee=$%.4f mid=%dc edge=%.1f%% unc=%.0f%% decay=%.0f%% src=%d)",
        "PAPER" if config.PAPER_MODE else "LIVE",
        direction, ticker, effective_contracts, price_cents,
        actual_stake, actual_fee, mid_price_cents,
        net_edge * 100, uncertainty_mult * 100, time_decay_mult * 100, source_count,
    )

    if config.PAPER_MODE:
        fill = await _simulate_paper_fill(effective_contracts, client_order_id)
    else:
        fill = await _execute_live_order(client, ticker, side, price_cents, effective_contracts, client_order_id)

    if not fill.success or fill.filled_contracts == 0:
        bot_logger.log_trade(
            ticker=ticker, market_title=market_title, direction=direction,
            entry_price_cents=price_cents, contracts=float(effective_contracts),
            stake_usd=actual_stake, fair_prob=fair_prob,
            fair_prob_sources=fair_prob_sources, gross_edge=gross_edge,
            net_edge=net_edge, fee_usd=actual_fee, kelly_fraction=adjusted_kelly,
            filled=False, filled_contracts=0.0, paper_mode=config.PAPER_MODE,
        )
        return False

    # Use actual fee from API if available (captures maker vs taker split)
    # Falls back to computed taker fee estimate if API didn't return fee data
    if fill.actual_total_fee is not None:
        filled_fee = fill.actual_total_fee
        fee_type = "actual (API)"
    else:
        filled_fee = compute_taker_fee(price_decimal, int(fill.filled_contracts), ticker)
        fee_type = "estimated (taker)"
    filled_cost = fill.filled_contracts * price_decimal + filled_fee
    _log.debug("Fill fee: $%.4f (%s)", filled_fee, fee_type)

    state_manager.add_position(
        ticker=ticker,
        direction=direction,
        entry_price_cents=price_cents,
        contracts=fill.filled_contracts,
        stake_usd=filled_cost,
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
    risk_manager.record_fill(filled_cost)

    bot_logger.log_trade(
        ticker=ticker, market_title=market_title, direction=direction,
        entry_price_cents=price_cents, contracts=float(effective_contracts),
        stake_usd=filled_cost, fair_prob=fair_prob,
        fair_prob_sources=fair_prob_sources, gross_edge=gross_edge,
        net_edge=net_edge, fee_usd=filled_fee, kelly_fraction=adjusted_kelly,
        filled=True, filled_contracts=fill.filled_contracts, paper_mode=config.PAPER_MODE,
    )

    mode = _fmt_mode()
    partial_note = " *(partial)*" if fill.partial else ""
    corr_note = f" *(corr: {corr_mult:.0%})*" if corr_mult < 1.0 else ""
    alert = (
        f"🎯 **BET PLACED** {mode}{partial_note}{corr_note}\n"
        f"**{ticker}** — {market_title[:80]}\n"
        f"**{direction}** @ **{price_cents}¢** (mid: {mid_price_cents}¢)\n"
        f"Contracts: **{fill.filled_contracts:.0f}** | Cost: **${filled_cost:.2f}** | Fee: **${filled_fee:.4f}**\n"
        f"Fair: **{fair_prob:.1%}** | Net Edge: **{net_edge:.1%}** | Gross: {gross_edge:.1%}\n"
        f"Sources: {', '.join(fair_prob_sources)} | Category: {category}\n"
        f"KL×Decay: {uncertainty_mult:.0%}×{time_decay_mult:.0%}"
    )
    await _send_discord(session, alert)
    return True


# ── close_position ─────────────────────────────────────────────────────────────
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
    client_order_id = position["client_order_id"]
    opened_at = position.get("opened_at", "")

    if resolution_pnl is not None:
        pnl_usd = resolution_pnl
    else:
        pnl_usd = (current_bid_cents - entry_cents) / 100.0 * contracts

    try:
        held_s = (datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)).total_seconds()
    except (ValueError, TypeError):
        held_s = 0.0

    # Live sell order (only for non-resolution closes)
    if not config.PAPER_MODE and reason != "resolved":
        sell_contracts = math.floor(contracts)
        if sell_contracts >= 1:
            await _execute_live_order(
                client=client, ticker=ticker,
                side="yes" if direction == "YES" else "no",
                price_cents=current_bid_cents,
                num_contracts=sell_contracts,
                client_order_id=str(uuid.uuid4()),
            )

    state_manager.remove_position(client_order_id)
    risk_manager.record_pnl(pnl_usd)

    bot_logger.log_position_closed(
        ticker=ticker, market_title=position.get("market_title", ticker),
        direction=direction, entry_price_cents=entry_cents,
        exit_price_cents=current_bid_cents, contracts=contracts,
        pnl_usd=pnl_usd, held_seconds=held_s,
        paper_mode=config.PAPER_MODE, reason=reason,
    )

    mode = _fmt_mode()
    held_str = f"{held_s/3600:.1f}h" if held_s >= 3600 else f"{held_s/60:.0f}m"
    emoji = "✅" if pnl_usd >= 0 else "🔴"
    reason_emoji = {"profit_take": "🎯", "stop_loss": "🛑", "resolved": "🏁"}.get(reason, "📋")
    alert = (
        f"{emoji} **CLOSED** {mode} {reason_emoji} {reason.upper()}\n"
        f"**{ticker}** | {direction} | Held: {held_str}\n"
        f"Entry: {entry_cents}¢ → Exit: {current_bid_cents}¢ | "
        f"Contracts: {contracts:.0f} | **P&L: {'+' if pnl_usd>=0 else ''}${pnl_usd:.2f}**"
    )
    await _send_discord(session, alert)
