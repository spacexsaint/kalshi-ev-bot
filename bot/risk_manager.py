"""
risk_manager.py — Circuit breakers, position limits, daily loss guard.

State is loaded from state_manager at startup.
All checks are synchronous (called from the async main loop via run_in_executor
or directly — they're fast enough to be non-blocking in practice).
"""

from __future__ import annotations

import logging
from typing import Optional

from bot import config
from bot import state_manager
from bot import logger as bot_logger

_log = logging.getLogger(__name__)


# ── Circuit breaker flag ───────────────────────────────────────────────────────
# Set to True when daily loss limit is hit; cleared only by reset_daily().
_halted: bool = False


# ── Public API ─────────────────────────────────────────────────────────────────

def can_trade() -> bool:
    """
    Return True if the bot is allowed to open new positions.

    Conditions that return False:
      1. _halted flag is set (daily loss limit already triggered)
      2. Daily PnL has crossed the loss threshold
      3. Open position count >= MAX_OPEN_POSITIONS
      4. PAPER_MODE is True — wait, paper mode DOES trade (paper trades are simulated)
         So paper mode does NOT block trading; it just flags orders as paper.
    """
    global _halted

    # Check if already halted
    if _halted:
        _log.debug("can_trade → False (circuit breaker active)")
        return False

    # Check daily loss limit
    daily_pnl = state_manager.get_daily_pnl()
    start_balance = state_manager.get_daily_start_balance()

    if start_balance > 0:
        loss_threshold = -(config.DAILY_LOSS_LIMIT_PCT * start_balance)
        if daily_pnl <= loss_threshold:
            _halted = True
            loss_pct = abs(daily_pnl / start_balance)
            current_balance = start_balance + daily_pnl
            _log.error(
                "CIRCUIT BREAKER TRIGGERED: daily PnL=%.2f (%.1f%% loss)",
                daily_pnl, loss_pct * 100,
            )
            bot_logger.log_circuit_breaker(
                reason=f"Daily loss limit reached: {loss_pct:.1%}",
                balance_usd=current_balance,
                daily_loss_usd=abs(daily_pnl),
                daily_loss_pct=loss_pct,
            )
            return False

    # Check position cap
    open_count = state_manager.open_position_count()
    if open_count >= config.MAX_OPEN_POSITIONS:
        _log.debug(
            "can_trade → False (positions=%d >= max=%d)",
            open_count, config.MAX_OPEN_POSITIONS,
        )
        return False

    return True


def is_halted() -> bool:
    """Return True if the daily circuit breaker has tripped."""
    return _halted


def record_fill(filled_usd: float) -> None:
    """
    Record a completed fill (called by executor after order fills).
    Updates PnL by the staked cost (negative, since we spent money).
    The actual PnL is reconciled via record_pnl when positions close.
    """
    # Staking money reduces "available" balance — track unrealised cost
    # (This is informational; true PnL only recorded on close.)
    _log.debug("Fill recorded: $%.2f staked", filled_usd)


def record_pnl(pnl_usd: float) -> None:
    """
    Record realised PnL when a position closes.
    Positive = profit, negative = loss.
    """
    state_manager.update_pnl(pnl_usd)
    _log.info("PnL recorded: %+.2f (daily total: %+.2f)", pnl_usd, state_manager.get_daily_pnl())


def reset_daily(current_balance_usd: float) -> None:
    """
    Reset daily tracking at UTC midnight.
    Clears the circuit breaker and updates start-of-day balance.
    """
    global _halted
    _halted = False
    state_manager.reset_daily(current_balance_usd)
    bot_logger.log_event(
        "daily_reset",
        f"Daily tracking reset. Start balance: ${current_balance_usd:.2f}",
        extra={"start_balance_usd": current_balance_usd},
    )
    _log.info("Daily reset. Start balance: $%.2f", current_balance_usd)


def get_stats() -> dict:
    """Return current risk stats snapshot."""
    daily_pnl = state_manager.get_daily_pnl()
    start_balance = state_manager.get_daily_start_balance()
    open_count = state_manager.open_position_count()
    daily_pnl_pct = (daily_pnl / start_balance) if start_balance > 0 else 0.0

    return {
        "halted": _halted,
        "can_trade": can_trade() if not _halted else False,
        "daily_pnl_usd": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "daily_start_balance": start_balance,
        "open_positions": open_count,
        "max_positions": config.MAX_OPEN_POSITIONS,
        "loss_limit_pct": config.DAILY_LOSS_LIMIT_PCT,
        "paper_mode": config.PAPER_MODE,
    }
