"""
state_manager.py — Persist bot state to disk between restarts.

Schema (data/state.json):
{
  "open_positions": [
    {
      "ticker": str,
      "direction": "YES" | "NO",
      "entry_price_cents": int,
      "contracts": float,         ← supports partial fills
      "stake_usd": float,
      "fair_prob_at_entry": float,
      "net_edge_at_entry": float,
      "opened_at": ISO8601,
      "client_order_id": str
    }
  ],
  "daily_start_balance": float,
  "daily_pnl": float,
  "last_reset_date": "YYYY-MM-DD",
  "match_cache_last_updated": ISO8601
}

Uses filelock to prevent corruption from concurrent writes.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone, date
from typing import Dict, List, Optional, Any

from filelock import FileLock

from bot import config


_LOCK_PATH = config.STATE_FILE + ".lock"
_file_lock = FileLock(_LOCK_PATH, timeout=10)


# ── Default state ──────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "open_positions": [],
        "daily_start_balance": 0.0,
        "daily_pnl": 0.0,
        "last_reset_date": date.today().isoformat(),
        "match_cache_last_updated": None,
    }


# ── Module-level in-memory state (loaded at startup) ──────────────────────────

_state: dict = _default_state()


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load() -> None:
    """Load state from disk into memory. Call once at startup."""
    global _state
    path = config.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not os.path.exists(path):
        _state = _default_state()
        save()
        return

    try:
        with _file_lock:
            with open(path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
        # Merge: use defaults for any missing keys
        merged = _default_state()
        merged.update(on_disk)
        _state = merged
    except (json.JSONDecodeError, OSError):
        _state = _default_state()
        save()


def save() -> None:
    """Persist current in-memory state to disk atomically."""
    path = config.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"

    with _file_lock:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, indent=2, default=str)
        os.replace(tmp_path, path)  # Atomic replace


def get_state() -> dict:
    """Return a deep copy of current state (read-only snapshot)."""
    return deepcopy(_state)


# ── Position management ────────────────────────────────────────────────────────

def add_position(
    ticker: str,
    direction: str,              # "YES" | "NO"
    entry_price_cents: int,
    contracts: float,
    stake_usd: float,
    fair_prob_at_entry: float,
    net_edge_at_entry: float,
    client_order_id: str,
) -> None:
    """Add a new open position and save to disk."""
    position = {
        "ticker": ticker,
        "direction": direction,
        "entry_price_cents": entry_price_cents,
        "contracts": contracts,
        "stake_usd": stake_usd,
        "fair_prob_at_entry": fair_prob_at_entry,
        "net_edge_at_entry": net_edge_at_entry,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "client_order_id": client_order_id,
    }
    _state["open_positions"].append(position)
    save()


def remove_position(client_order_id: str) -> Optional[dict]:
    """
    Remove a position by client_order_id.

    Returns:
        The removed position dict, or None if not found.
    """
    positions = _state["open_positions"]
    for i, pos in enumerate(positions):
        if pos.get("client_order_id") == client_order_id:
            removed = positions.pop(i)
            save()
            return removed
    return None


def remove_position_by_ticker(ticker: str) -> Optional[dict]:
    """Remove a position by ticker (removes first match)."""
    positions = _state["open_positions"]
    for i, pos in enumerate(positions):
        if pos.get("ticker") == ticker:
            removed = positions.pop(i)
            save()
            return removed
    return None


def get_open_positions() -> List[dict]:
    """Return a copy of all open positions."""
    return deepcopy(_state["open_positions"])


def get_position(ticker: str) -> Optional[dict]:
    """Find a position by ticker."""
    for pos in _state["open_positions"]:
        if pos.get("ticker") == ticker:
            return deepcopy(pos)
    return None


def open_position_count() -> int:
    """Return the number of currently open positions."""
    return len(_state["open_positions"])


def open_tickers() -> List[str]:
    """Return list of tickers with open positions."""
    return [p["ticker"] for p in _state["open_positions"]]


# ── PnL management ─────────────────────────────────────────────────────────────

def update_pnl(delta_usd: float) -> None:
    """Add delta to daily PnL and save."""
    _state["daily_pnl"] = _state.get("daily_pnl", 0.0) + delta_usd
    save()


def get_daily_pnl() -> float:
    return _state.get("daily_pnl", 0.0)


def get_daily_start_balance() -> float:
    return _state.get("daily_start_balance", 0.0)


def set_daily_start_balance(balance_usd: float) -> None:
    _state["daily_start_balance"] = balance_usd
    save()


# ── Daily reset ────────────────────────────────────────────────────────────────

def reset_daily(current_balance_usd: float) -> None:
    """
    Reset daily tracking. Called at UTC midnight.
    Sets daily_start_balance to current balance and zeroes PnL.
    Does NOT clear open positions.
    """
    today = date.today().isoformat()
    _state["daily_start_balance"] = current_balance_usd
    _state["daily_pnl"] = 0.0
    _state["last_reset_date"] = today
    save()


def needs_daily_reset() -> bool:
    """Return True if the last reset date is before today (UTC)."""
    last = _state.get("last_reset_date")
    if not last:
        return True
    return last < date.today().isoformat()


def get_last_reset_date() -> str:
    return _state.get("last_reset_date", "")


# ── Match cache metadata ───────────────────────────────────────────────────────

def update_match_cache_ts() -> None:
    _state["match_cache_last_updated"] = datetime.now(timezone.utc).isoformat()
    save()


def get_match_cache_ts() -> Optional[str]:
    return _state.get("match_cache_last_updated")
