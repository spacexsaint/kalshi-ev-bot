"""
market_matcher.py — Fuzzy title matching between Kalshi and external sources.

Algorithm:
  1. Preprocess: lowercase, strip punctuation, normalize whitespace
  2. Primary score: token_sort_ratio (handles word-order differences)
  3. Secondary: partial_ratio
  4. Final score = 0.7 × token_sort_ratio + 0.3 × partial_ratio
  5. Match if final_score >= FUZZY_MATCH_THRESHOLD (0.75)
  6. Date validation: resolution dates within DATE_MATCH_TOLERANCE_DAYS
  7. Scores in [FUZZY_LOW_CONF_MIN, FUZZY_MATCH_THRESHOLD): log, don't trade
  8. Cache in data/match_cache.json, TTL = MATCH_CACHE_TTL_HOURS

FIX (2026-04-09): Cache file I/O is now done OUTSIDE the lock scope.
  Previously _save_cache() was called while holding _cache_lock, meaning
  any slow disk flush (>50ms) blocked all concurrent find_match threads.
  Fix: copy the data under the lock, release the lock, then write to disk.
"""

import json
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from bot import config
from bot import logger


@dataclass
class Candidate:
    title: str
    probability: float
    close_date: Optional[datetime]
    source: str
    market_id: str


@dataclass
class Match:
    kalshi_title: str
    kalshi_ticker: str
    kalshi_close_date: Optional[datetime]
    candidate: Candidate
    score: float
    token_sort_ratio: float
    partial_ratio: float
    matched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Cache ──────────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: Dict[str, dict] = {}
_cache_loaded_at: Optional[datetime] = None


def _load_cache() -> None:
    """Load cache from disk. Must be called with _cache_lock held."""
    global _cache, _cache_loaded_at
    path = config.MATCH_CACHE_FILE
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _cache = data.get("matches", {})
        ts = data.get("updated_at")
        _cache_loaded_at = datetime.fromisoformat(ts) if ts else None
    except (json.JSONDecodeError, OSError):
        _cache = {}
        _cache_loaded_at = None


def _save_cache_to_disk(snapshot: dict) -> None:
    """
    Write cache snapshot to disk. Called WITHOUT holding _cache_lock.

    Uses atomic write (write to .tmp, then os.replace) so readers never
    see a partial file. Lock is released before I/O to avoid blocking
    concurrent find_match calls during slow disk flushes.
    """
    path = config.MATCH_CACHE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"updated_at": datetime.now(timezone.utc).isoformat(), "matches": snapshot},
                fh, indent=2, default=str,
            )
        os.replace(tmp, path)
    except OSError:
        pass  # Non-fatal — cache is in memory, disk write is best-effort


def _cache_is_stale() -> bool:
    """Must be called with _cache_lock held."""
    if _cache_loaded_at is None:
        return True
    return datetime.now(timezone.utc) - _cache_loaded_at > timedelta(hours=config.MATCH_CACHE_TTL_HOURS)


def _cache_key(ticker: str, source: str) -> str:
    return f"{ticker}::{source}"


# ── Text preprocessing ─────────────────────────────────────────────────────────
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _preprocess(title: str) -> str:
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", title.lower())).strip()


# ── Date validation ────────────────────────────────────────────────────────────
def _dates_compatible(
    a: Optional[datetime],
    b: Optional[datetime],
    tolerance_days: int = config.DATE_MATCH_TOLERANCE_DAYS,
) -> bool:
    if a is None or b is None:
        return True
    return abs((a - b).total_seconds()) <= tolerance_days * 86_400


# ── Scoring ────────────────────────────────────────────────────────────────────
def _compute_score(a: str, b: str) -> Tuple[float, float, float]:
    """Return (final, token_sort_ratio, partial_ratio), all in [0,1]."""
    pa, pb = _preprocess(a), _preprocess(b)
    tsr = fuzz.token_sort_ratio(pa, pb) / 100.0
    pr = fuzz.partial_ratio(pa, pb) / 100.0
    return 0.7 * tsr + 0.3 * pr, tsr, pr


# ── Public API ─────────────────────────────────────────────────────────────────
def initialise() -> None:
    with _cache_lock:
        if _cache_is_stale():
            _load_cache()


def find_match(
    kalshi_ticker: str,
    kalshi_title: str,
    kalshi_close_date: Optional[datetime],
    candidates: List[Candidate],
) -> Optional[Match]:
    """
    Find best-matching external market. Returns None if below threshold.

    Thread-safe. Lock is held only for in-memory reads/writes.
    Disk I/O happens after the lock is released.
    """
    # Refresh stale cache under lock
    with _cache_lock:
        if _cache_is_stale():
            _load_cache()

    if not candidates:
        return None

    # Score all candidates (no lock needed — read-only)
    best_score = -1.0
    best_candidate: Optional[Candidate] = None
    best_tsr = best_pr = 0.0

    for candidate in candidates:
        if not _dates_compatible(kalshi_close_date, candidate.close_date):
            continue
        score, tsr, pr = _compute_score(kalshi_title, candidate.title)
        if score > best_score:
            best_score, best_candidate, best_tsr, best_pr = score, candidate, tsr, pr

    if best_candidate is None:
        return None

    # Low-confidence: log and reject
    if config.FUZZY_LOW_CONF_MIN <= best_score < config.FUZZY_MATCH_THRESHOLD:
        logger.log_low_confidence_match(
            kalshi_title=kalshi_title,
            kalshi_close_date=kalshi_close_date.isoformat() if kalshi_close_date else "unknown",
            matched_title=best_candidate.title,
            source=best_candidate.source,
            score=best_score,
            token_sort_ratio=best_tsr,
            partial_ratio=best_pr,
        )
        return None

    if best_score < config.FUZZY_LOW_CONF_MIN:
        return None

    match = Match(
        kalshi_title=kalshi_title,
        kalshi_ticker=kalshi_ticker,
        kalshi_close_date=kalshi_close_date,
        candidate=best_candidate,
        score=best_score,
        token_sort_ratio=best_tsr,
        partial_ratio=best_pr,
    )

    # Update cache in memory (fast, under lock), write disk outside lock
    key = _cache_key(kalshi_ticker, best_candidate.source)
    with _cache_lock:
        _cache[key] = asdict(match)
        snapshot = dict(_cache)  # Shallow copy for disk write

    # Disk I/O outside lock — non-blocking for other threads
    _save_cache_to_disk(snapshot)

    return match


def invalidate_cache() -> None:
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = {}
        _cache_loaded_at = None
    path = config.MATCH_CACHE_FILE
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
