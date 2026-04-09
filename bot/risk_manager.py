"""
risk_manager.py — Circuit breakers, position limits, daily loss guard.

═══════════════════════════════════════════════════════════════════
AUDIT IMPROVEMENTS (2026-04-08):
═══════════════════════════════════════════════════════════════════

[NEW] Correlation-aware position sizing:
  If multiple open positions share the same category (e.g., all "Fed rate
  cut" markets), they are correlated bets. The Kelly criterion assumes
  independent bets — violating this assumption leads to over-betting.

  Fix: Track open position categories. If a category already has
  >= MAX_POSITIONS_PER_CATEGORY open positions, block the new bet entirely.
  If it has exactly 1, apply CORRELATED_BET_SIZE_PENALTY (50% size reduction).

  Source: Galekwa et al. (IEEE ACCESS 2026) — "betting funds with accurate
  models but no risk management discipline consistently fail."

[IMPROVED] can_trade() now returns a reason string for logging.
[IMPROVED] get_position_category() for correlation detection.
═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from bot import config
from bot import state_manager
from bot import logger as bot_logger

_log = logging.getLogger(__name__)

# ── Circuit breaker flag ───────────────────────────────────────────────────────
_halted: bool = False
_pnl_warning_sent: bool = False  # Track whether early warning has been sent today


# ── Category extraction ────────────────────────────────────────────────────────

# Keywords that identify correlated market categories
_CATEGORY_PATTERNS: Dict[str, List[str]] = {
    "fed_rates": ["fed", "federal reserve", "rate cut", "interest rate", "fomc", "powell"],
    "inflation": ["cpi", "inflation", "pce", "price index"],
    "unemployment": ["unemployment", "jobless", "nonfarm", "payroll"],
    "election": ["election", "president", "senate", "congress", "ballot", "vote"],
    "trump": ["trump", "donald"],
    "biden": ["biden"],
    "btc": ["bitcoin", "btc"],
    "eth": ["ethereum", "eth"],
    "crypto": ["crypto", "coinbase", "binance"],
    "nba": ["nba", "basketball", "finals", "lakers", "celtics", "warriors"],
    "nfl": ["nfl", "football", "super bowl", "chiefs", "eagles"],
    "mlb": ["mlb", "baseball", "world series"],
    "sp500": ["s&p", "sp500", "inx", "stock market"],
    "gdp": ["gdp", "recession", "growth"],
    "tech": ["microsoft", "apple", "google", "amazon", "meta", "tesla", "nvidia", "semiconductor"],
    "ai": ["artificial intelligence", "openai", "anthropic", "llm", "gpt", "chatgpt", "gemini"],
    "science": ["climate", "temperature", "nasa", "spacex", "nuclear", "vaccine"],
    "health": ["fda", "drug approval", "covid", "pandemic", "flu", "cancer"],
}


def get_position_category(title: str) -> str:
    """
    Extract a correlation category from a market title.
    Returns "uncategorized" if no known category detected.
    """
    title_lower = title.lower()
    for category, keywords in _CATEGORY_PATTERNS.items():
        if any(kw in title_lower for kw in keywords):
            return category
    return "uncategorized"


def count_open_positions_in_category(category: str) -> int:
    """Count how many open positions share the given category."""
    if category == "uncategorized":
        return 0   # Don't penalise uncategorised markets
    count = 0
    for pos in state_manager.get_open_positions():
        pos_title = pos.get("ticker", "") + " " + pos.get("direction", "")
        # Use ticker as a proxy — real category stored when position opened
        pos_category = pos.get("category", get_position_category(pos.get("market_title", pos.get("ticker", ""))))
        if pos_category == category:
            count += 1
    return count


def get_correlation_stake_multiplier(market_title: str) -> Tuple[float, str]:
    """
    Return a stake multiplier and category based on correlation risk.

    Returns:
        (multiplier, category)
        multiplier = 1.0  → no penalty
        multiplier = 0.50 → 1 existing correlated position (halve stake)
        multiplier = 0.0  → blocked (>= MAX_POSITIONS_PER_CATEGORY)
    """
    category = get_position_category(market_title)
    count = count_open_positions_in_category(category)

    if count >= config.MAX_POSITIONS_PER_CATEGORY:
        return 0.0, category
    if count == 1:
        return config.CORRELATED_BET_SIZE_PENALTY, category
    return 1.0, category


# ── Public API ─────────────────────────────────────────────────────────────────

def can_trade(market_title: str = "") -> Tuple[bool, str]:
    """
    Return (allowed, reason) for whether the bot can open a new position.

    Conditions that return False:
      1. _halted flag set (daily loss limit already triggered)
      2. Daily PnL crossed the loss threshold
      3. Open position count >= MAX_OPEN_POSITIONS
      4. Correlation block: category already at MAX_POSITIONS_PER_CATEGORY

    Args:
        market_title: Optional market title for correlation check.

    Returns:
        (True, "ok") or (False, reason_string)
    """
    global _halted, _pnl_warning_sent

    if _halted:
        return False, "circuit_breaker_halted"

    daily_pnl = state_manager.get_daily_pnl()
    start_balance = state_manager.get_daily_start_balance()

    if start_balance > 0:
        loss_threshold = -(config.DAILY_LOSS_LIMIT_PCT * start_balance)

        # Early warning at 67% of circuit breaker threshold (once per day).
        # Alerts operator before full halt so they can intervene.
        warning_threshold = loss_threshold * 0.67
        if daily_pnl <= warning_threshold and not _pnl_warning_sent:
            _pnl_warning_sent = True
            loss_pct = abs(daily_pnl / start_balance)
            _log.warning(
                "PNL WARNING: daily PnL=%.2f (%.1f%% loss) — approaching circuit breaker at %.1f%%",
                daily_pnl, loss_pct * 100, config.DAILY_LOSS_LIMIT_PCT * 100,
            )
            bot_logger.log_event(
                "pnl_warning",
                f"Daily P&L warning: {loss_pct:.1%} loss — approaching {config.DAILY_LOSS_LIMIT_PCT:.0%} circuit breaker",
                extra={"daily_pnl": daily_pnl, "loss_pct": loss_pct,
                       "warning_threshold": warning_threshold, "halt_threshold": loss_threshold},
                severity="warning",
            )

        if daily_pnl <= loss_threshold:
            _halted = True
            loss_pct = abs(daily_pnl / start_balance)
            current_balance = start_balance + daily_pnl
            _log.error(
                "CIRCUIT BREAKER: daily PnL=%.2f (%.1f%% loss)",
                daily_pnl, loss_pct * 100,
            )
            bot_logger.log_circuit_breaker(
                reason=f"Daily loss limit reached: {loss_pct:.1%}",
                balance_usd=current_balance,
                daily_loss_usd=abs(daily_pnl),
                daily_loss_pct=loss_pct,
            )
            return False, "daily_loss_limit"

    open_count = state_manager.open_position_count()
    if open_count >= config.MAX_OPEN_POSITIONS:
        return False, f"position_cap_{open_count}/{config.MAX_OPEN_POSITIONS}"

    # Correlation check
    if market_title:
        mult, category = get_correlation_stake_multiplier(market_title)
        if mult == 0.0:
            return False, f"correlation_block_{category}"

    return True, "ok"


def is_halted() -> bool:
    return _halted


def record_fill(filled_usd: float) -> None:
    _log.debug("Fill recorded: $%.2f staked", filled_usd)


def record_pnl(pnl_usd: float) -> None:
    state_manager.update_pnl(pnl_usd)
    _log.info(
        "PnL recorded: %+.2f (daily total: %+.2f)",
        pnl_usd, state_manager.get_daily_pnl(),
    )


def reset_daily(current_balance_usd: float) -> None:
    global _halted, _pnl_warning_sent
    _halted = False
    _pnl_warning_sent = False
    state_manager.reset_daily(current_balance_usd)
    bot_logger.log_event(
        "daily_reset",
        f"Daily tracking reset. Start balance: ${current_balance_usd:.2f}",
        extra={"start_balance_usd": current_balance_usd},
    )
    _log.info("Daily reset. Start balance: $%.2f", current_balance_usd)


def get_stats() -> dict:
    daily_pnl = state_manager.get_daily_pnl()
    start_balance = state_manager.get_daily_start_balance()
    open_count = state_manager.open_position_count()
    daily_pnl_pct = (daily_pnl / start_balance) if start_balance > 0 else 0.0
    tradeable, reason = can_trade()

    return {
        "halted": _halted,
        "can_trade": tradeable,
        "can_trade_reason": reason,
        "daily_pnl_usd": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "daily_start_balance": start_balance,
        "open_positions": open_count,
        "max_positions": config.MAX_OPEN_POSITIONS,
        "loss_limit_pct": config.DAILY_LOSS_LIMIT_PCT,
        "paper_mode": config.PAPER_MODE,
    }
