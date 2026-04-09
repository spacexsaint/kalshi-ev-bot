"""
state_manager.py — Persist bot state to disk between restarts.

Schema (data/state.json):
{
  "open_positions": [
    {
      "ticker": str,
      "market_title": str,           ← NEW: for category detection
      "category": str,               ← NEW: correlated position detection
      "direction": "YES" | "NO",
      "entry_price_cents": int,
      "exec_price_cents": int,       ← NEW: actual execution price (ask)
      "mid_price_cents": int,        ← NEW: midpoint at entry (for true cost tracking)
      "contracts": float,
      "stake_usd": float,
      "fair_prob_at_entry": float,
      "net_edge_at_entry": float,
      "gross_edge_at_entry": float,  ← NEW
      "source_count": int,           ← NEW: number of sources used
      "sources": [str],              ← NEW: which sources matched
      "uncertainty_mult": float,     ← NEW: KL uncertainty penalty applied
      "time_decay_mult": float,      ← NEW: time-decay penalty applied
      "opened_at": ISO8601,
      "client_order_id": str
    }
  ],
  "daily_start_balance": float,
  "daily_pnl": float,
  "last_reset_date": "YYYY-MM-DD",
  "match_cache_last_updated": ISO8601
}
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


def _default_state() -> dict:
    return {
        "open_positions": [],
        "daily_start_balance": 0.0,
        "daily_pnl": 0.0,
        "last_reset_date": date.today().isoformat(),
        "match_cache_last_updated": None,
    }


_state: dict = _default_state()


def load() -> None:
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
        merged = _default_state()
        merged.update(on_disk)
        _state = merged
    except (json.JSONDecodeError, OSError):
        _state = _default_state()
        save()


def save() -> None:
    path = config.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with _file_lock:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, indent=2, default=str)
        os.replace(tmp_path, path)


def get_state() -> dict:
    return deepcopy(_state)


# ── Position management ────────────────────────────────────────────────────────

def add_position(
    ticker: str,
    direction: str,
    entry_price_cents: int,
    contracts: float,
    stake_usd: float,
    fair_prob_at_entry: float,
    net_edge_at_entry: float,
    client_order_id: str,
    # New fields
    market_title: str = "",
    category: str = "uncategorized",
    exec_price_cents: int = 0,
    mid_price_cents: int = 0,
    gross_edge_at_entry: float = 0.0,
    source_count: int = 1,
    sources: Optional[List[str]] = None,
    uncertainty_mult: float = 1.0,
    time_decay_mult: float = 1.0,
) -> None:
    position = {
        "ticker": ticker,
        "market_title": market_title,
        "category": category,
        "direction": direction,
        "entry_price_cents": entry_price_cents,
        "exec_price_cents": exec_price_cents or entry_price_cents,
        "mid_price_cents": mid_price_cents or entry_price_cents,
        "contracts": contracts,
        "stake_usd": stake_usd,
        "fair_prob_at_entry": fair_prob_at_entry,
        "net_edge_at_entry": net_edge_at_entry,
        "gross_edge_at_entry": gross_edge_at_entry,
        "source_count": source_count,
        "sources": sources or [],
        "uncertainty_mult": uncertainty_mult,
        "time_decay_mult": time_decay_mult,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "client_order_id": client_order_id,
    }
    _state["open_positions"].append(position)
    save()


def remove_position(client_order_id: str) -> Optional[dict]:
    positions = _state["open_positions"]
    for i, pos in enumerate(positions):
        if pos.get("client_order_id") == client_order_id:
            removed = positions.pop(i)
            save()
            return removed
    return None


def remove_position_by_ticker(ticker: str) -> Optional[dict]:
    positions = _state["open_positions"]
    for i, pos in enumerate(positions):
        if pos.get("ticker") == ticker:
            removed = positions.pop(i)
            save()
            return removed
    return None


def get_open_positions() -> List[dict]:
    return deepcopy(_state["open_positions"])


def get_position(ticker: str) -> Optional[dict]:
    for pos in _state["open_positions"]:
        if pos.get("ticker") == ticker:
            return deepcopy(pos)
    return None


def open_position_count() -> int:
    return len(_state["open_positions"])


def open_tickers() -> List[str]:
    return [p["ticker"] for p in _state["open_positions"]]


# ── PnL management ─────────────────────────────────────────────────────────────

def update_pnl(delta_usd: float) -> None:
    _state["daily_pnl"] = _state.get("daily_pnl", 0.0) + delta_usd
    save()


def get_daily_pnl() -> float:
    return _state.get("daily_pnl", 0.0)


def get_daily_start_balance() -> float:
    return _state.get("daily_start_balance", 0.0)


def set_daily_start_balance(balance_usd: float) -> None:
    _state["daily_start_balance"] = balance_usd
    save()


def reset_daily(current_balance_usd: float) -> None:
    today = date.today().isoformat()
    _state["daily_start_balance"] = current_balance_usd
    _state["daily_pnl"] = 0.0
    _state["last_reset_date"] = today
    save()


def needs_daily_reset() -> bool:
    last = _state.get("last_reset_date")
    if not last:
        return True
    return last < date.today().isoformat()


def get_last_reset_date() -> str:
    return _state.get("last_reset_date", "")


def update_match_cache_ts() -> None:
    _state["match_cache_last_updated"] = datetime.now(timezone.utc).isoformat()
    save()


def get_match_cache_ts() -> Optional[str]:
    return _state.get("match_cache_last_updated")
