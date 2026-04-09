"""
logger.py — Structured JSON (JSONL) logging.

All log files are newline-delimited JSON in logs/.
  logs/trades.jsonl                — every executed trade
  logs/events.jsonl                — startup, shutdown, circuit breakers (with severity)
  logs/api.jsonl                   — every API call (method, endpoint, status, latency_ms)
  logs/low_confidence_matches.jsonl — fuzzy matches in the 0.65-0.74 grey zone
  logs/brier_scores.jsonl          — calibration tracking: fair_prob vs resolved outcome
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Thread-safe per-file locks ─────────────────────────────────────────────────
_locks: Dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    with _locks_meta:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_MAX_LOG_BYTES: int = 10 * 1024 * 1024  # 10 MB max per JSONL file
_TRIM_KEEP_LINES: int = 25_000          # After trim, keep this many recent lines
_write_count: Dict[str, int] = {}


def _write(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON object as a line (JSONL format). Trims when file exceeds _MAX_LOG_BYTES."""
    _ensure_dir(path)
    lock = _get_lock(path)
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        # Check file size every 500 writes to avoid stat() on every append
        _write_count[path] = _write_count.get(path, 0) + 1
        if _write_count[path] % 500 == 0:
            try:
                if os.path.getsize(path) > _MAX_LOG_BYTES:
                    with open(path, "r", encoding="utf-8") as fh:
                        lines = fh.readlines()
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.writelines(lines[-_TRIM_KEEP_LINES:])
            except OSError:
                pass  # Non-fatal — trimming is best-effort


# ── Trade logging ──────────────────────────────────────────────────────────────

def log_trade(
    *,
    ticker: str,
    market_title: str,
    direction: str,
    entry_price_cents: int,
    contracts: float,
    stake_usd: float,
    fair_prob: float,
    fair_prob_sources: List[str],
    gross_edge: float,
    net_edge: float,
    fee_usd: float,
    kelly_fraction: float,
    filled: bool,
    filled_contracts: float,
    paper_mode: bool,
    log_path: str = "logs/trades.jsonl",
) -> None:
    _write(log_path, {
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
    })


# ── Event logging (with severity) ─────────────────────────────────────────────

def log_event(
    event_type: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
    severity: str = "info",   # "debug" | "info" | "warning" | "error" | "critical"
    log_path: str = "logs/events.jsonl",
) -> None:
    _write(log_path, {
        "ts": _now_iso(),
        "severity": severity,
        "event_type": event_type,
        "message": message,
        **(extra or {}),
    })


# ── API call logging ───────────────────────────────────────────────────────────

def log_api_call(
    *,
    method: str,
    endpoint: str,
    status_code: int,
    latency_ms: float,
    error: Optional[str] = None,
    log_path: str = "logs/api.jsonl",
) -> None:
    _write(log_path, {
        "ts": _now_iso(),
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
        "error": error,
    })


# ── Low-confidence match logging ───────────────────────────────────────────────

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
    _write(log_path, {
        "ts": _now_iso(),
        "kalshi_title": kalshi_title,
        "kalshi_close_date": kalshi_close_date,
        "matched_title": matched_title,
        "source": source,
        "score": round(score, 4),
        "token_sort_ratio": round(token_sort_ratio, 4),
        "partial_ratio": round(partial_ratio, 4),
    })


# ── Position closed logging ────────────────────────────────────────────────────

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
    reason: str = "unknown",
    log_path: str = "logs/events.jsonl",
) -> None:
    log_event(
        "position_closed",
        f"Closed {direction} on {ticker}: PnL={pnl_usd:+.2f} reason={reason}",
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
            "reason": reason,
        },
        severity="info",
        log_path=log_path,
    )


# ── Circuit breaker logging ────────────────────────────────────────────────────

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
        severity="critical",
        log_path=log_path,
    )


# ── Brier score / calibration tracking ────────────────────────────────────────

def log_brier_score(
    *,
    ticker: str,
    market_title: str,
    fair_prob_at_entry: float,
    sources: List[str],
    resolved_yes: bool,
    brier_score: float,
    log_path: str = "logs/brier_scores.jsonl",
) -> None:
    """
    Log a Brier score observation for calibration tracking.

    Brier score = (fair_prob - outcome)^2 where outcome ∈ {0, 1}.
    Lower = better calibrated. Perfect calibration = 0.00.
    A fair coin would score 0.25.

    Over time, the running mean of log_brier_score tells us whether our
    external probability estimates are actually predictive. If mean > 0.25,
    our sources are WORSE than random and we should re-evaluate.
    """
    _write(log_path, {
        "ts": _now_iso(),
        "ticker": ticker,
        "market_title": market_title,
        "fair_prob_at_entry": round(fair_prob_at_entry, 4),
        "sources": sources,
        "resolved_yes": resolved_yes,
        "outcome": 1 if resolved_yes else 0,
        "brier_score": round(brier_score, 6),
        # Running interpretation: < 0.10 excellent, 0.10-0.20 good, > 0.25 random
    })
