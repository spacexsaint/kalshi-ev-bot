"""
logger.py — Structured JSON (JSONL) logging for all bot events.

All log files are written to /logs/ as newline-delimited JSON.
Files:
  logs/trades.jsonl              — every executed trade
  logs/events.jsonl              — startup, shutdown, circuit breakers
  logs/api.jsonl                 — every API call (method, endpoint, status, latency_ms)
  logs/low_confidence_matches.jsonl — fuzzy matches in 0.65–0.74 range
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# Thread-safe lock per log file
_locks: Dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    with _locks_meta:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON object as a line to the given JSONL file."""
    _ensure_dir(path)
    lock = _get_lock(path)
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


# ── Public log functions ───────────────────────────────────────────────────────

def log_trade(
    *,
    ticker: str,
    market_title: str,
    direction: str,                      # "YES" | "NO"
    entry_price_cents: int,
    contracts: float,
    stake_usd: float,
    fair_prob: float,
    fair_prob_sources: List[str],        # ["manifold","predictit"] | ["manifold"] | ["predictit"]
    gross_edge: float,
    net_edge: float,
    fee_usd: float,
    kelly_fraction: float,
    filled: bool,
    filled_contracts: float,
    paper_mode: bool,
    log_path: str = "logs/trades.jsonl",
) -> None:
    record = {
        "ts": _now_iso(),
        "ticker": ticker,
        "market_title": market_title,
        "direction": direction,
        "entry_price_cents": entry_price_cents,
        "contracts": contracts,
        "stake_usd": stake_usd,
        "fair_prob": fair_prob,
        "fair_prob_sources": fair_prob_sources,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "fee_usd": fee_usd,
        "kelly_fraction": kelly_fraction,
        "filled": filled,
        "filled_contracts": filled_contracts,
        "paper_mode": paper_mode,
    }
    _write(log_path, record)


def log_event(
    event_type: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
    log_path: str = "logs/events.jsonl",
) -> None:
    record = {
        "ts": _now_iso(),
        "event_type": event_type,
        "message": message,
        **(extra or {}),
    }
    _write(log_path, record)


def log_api_call(
    *,
    method: str,
    endpoint: str,
    status_code: int,
    latency_ms: float,
    error: Optional[str] = None,
    log_path: str = "logs/api.jsonl",
) -> None:
    record = {
        "ts": _now_iso(),
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
        "error": error,
    }
    _write(log_path, record)


def log_low_confidence_match(
    *,
    kalshi_title: str,
    kalshi_close_date: str,
    matched_title: str,
    source: str,
    score: float,
    token_sort_ratio: float,
    partial_ratio: float,
    log_path: str = "logs/low_confidence_matches.jsonl",
) -> None:
    record = {
        "ts": _now_iso(),
        "kalshi_title": kalshi_title,
        "kalshi_close_date": kalshi_close_date,
        "matched_title": matched_title,
        "source": source,
        "score": round(score, 4),
        "token_sort_ratio": round(token_sort_ratio, 4),
        "partial_ratio": round(partial_ratio, 4),
    }
    _write(log_path, record)


def log_position_closed(
    *,
    ticker: str,
    market_title: str,
    direction: str,
    entry_price_cents: int,
    exit_price_cents: int,
    contracts: float,
    pnl_usd: float,
    held_seconds: float,
    paper_mode: bool,
    log_path: str = "logs/events.jsonl",
) -> None:
    log_event(
        "position_closed",
        f"Closed {direction} position on {ticker}: PnL=${pnl_usd:.2f}",
        extra={
            "ticker": ticker,
            "market_title": market_title,
            "direction": direction,
            "entry_price_cents": entry_price_cents,
            "exit_price_cents": exit_price_cents,
            "contracts": contracts,
            "pnl_usd": pnl_usd,
            "held_seconds": round(held_seconds, 1),
            "paper_mode": paper_mode,
        },
        log_path=log_path,
    )


def log_circuit_breaker(
    *,
    reason: str,
    balance_usd: float,
    daily_loss_usd: float,
    daily_loss_pct: float,
    log_path: str = "logs/events.jsonl",
) -> None:
    log_event(
        "circuit_breaker",
        f"TRADING HALTED — {reason}",
        extra={
            "reason": reason,
            "balance_usd": balance_usd,
            "daily_loss_usd": daily_loss_usd,
            "daily_loss_pct": daily_loss_pct,
        },
        log_path=log_path,
    )
