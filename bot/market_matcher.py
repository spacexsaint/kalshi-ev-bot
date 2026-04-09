"""
market_matcher.py — Fuzzy title matching between Kalshi and Manifold/PredictIt.

Algorithm:
  1. Preprocess: lowercase, strip punctuation, normalize whitespace
  2. Primary score: token_sort_ratio (handles word-order differences)
  3. Secondary score: partial_ratio
  4. Final score = 0.7 × token_sort_ratio + 0.3 × partial_ratio
  5. Only return a match if final_score >= FUZZY_MATCH_THRESHOLD (0.75)
  6. Additionally validate: resolution dates within 7 days of each other
  7. Scores in [0.65, 0.74) → log to low_confidence_matches.jsonl, return None
  8. Cache all matches in /data/match_cache.json (refresh every 6 hours)

Uses rapidfuzz library (pip install rapidfuzz).
"""

import json
import os
import re
import string
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from bot import config
from bot import logger


@dataclass
class Candidate:
    """A market from an external source (Manifold or PredictIt)."""
    title: str
    probability: float               # 0–1
    close_date: Optional[datetime]   # None if unknown
    source: str                      # "manifold" | "predictit"
    market_id: str                   # External market identifier


@dataclass
class Match:
    """A confirmed match between a Kalshi market and an external candidate."""
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
_cache: Dict[str, dict] = {}       # key → serialised Match dict
_cache_loaded_at: Optional[datetime] = None


def _load_cache() -> None:
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


def _save_cache() -> None:
    path = config.MATCH_CACHE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "matches": _cache,
            },
            fh,
            indent=2,
            default=str,
        )


def _cache_is_stale() -> bool:
    if _cache_loaded_at is None:
        return True
    age = datetime.now(timezone.utc) - _cache_loaded_at
    return age > timedelta(hours=config.MATCH_CACHE_TTL_HOURS)


def _cache_key(kalshi_ticker: str, source: str) -> str:
    return f"{kalshi_ticker}::{source}"


# ── Text preprocessing ─────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _preprocess(title: str) -> str:
    """Lowercase → strip punctuation → collapse whitespace."""
    lowered = title.lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    normalised = _WS_RE.sub(" ", no_punct).strip()
    return normalised


# ── Date validation ────────────────────────────────────────────────────────────

def _dates_compatible(
    kalshi_close: Optional[datetime],
    candidate_close: Optional[datetime],
    tolerance_days: int = config.DATE_MATCH_TOLERANCE_DAYS,
) -> bool:
    """Return True if dates are within tolerance or either is unknown."""
    if kalshi_close is None or candidate_close is None:
        return True   # Can't invalidate what we don't know
    diff = abs((kalshi_close - candidate_close).total_seconds())
    return diff <= tolerance_days * 86_400


# ── Scoring ────────────────────────────────────────────────────────────────────

def _compute_score(title_a: str, title_b: str) -> Tuple[float, float, float]:
    """
    Return (final_score, token_sort_ratio, partial_ratio) — all in [0, 1].
    final_score = 0.7 × token_sort_ratio + 0.3 × partial_ratio
    """
    a = _preprocess(title_a)
    b = _preprocess(title_b)
    tsr = fuzz.token_sort_ratio(a, b) / 100.0
    pr = fuzz.partial_ratio(a, b) / 100.0
    final = 0.7 * tsr + 0.3 * pr
    return final, tsr, pr


# ── Public API ─────────────────────────────────────────────────────────────────

def initialise() -> None:
    """Load cache from disk. Call once at bot startup."""
    with _cache_lock:
        _load_cache()


def find_match(
    kalshi_ticker: str,
    kalshi_title: str,
    kalshi_close_date: Optional[datetime],
    candidates: List[Candidate],
) -> Optional[Match]:
    """
    Find the best-matching external market for a Kalshi market.

    Args:
        kalshi_ticker:     Kalshi market ticker (e.g., "KXFED-26NOV-T5.25")
        kalshi_title:      Human-readable title of the Kalshi market
        kalshi_close_date: When the Kalshi market closes (UTC-aware or None)
        candidates:        List of Candidate objects from one source

    Returns:
        Match object if a confident match is found; None otherwise.
        Low-confidence matches (0.65–0.74) are logged but NOT returned.
    """
    with _cache_lock:
        if _cache_is_stale():
            _load_cache()

    # Score all candidates
    best_score = -1.0
    best_candidate: Optional[Candidate] = None
    best_tsr = 0.0
    best_pr = 0.0

    for candidate in candidates:
        score, tsr, pr = _compute_score(kalshi_title, candidate.title)

        if not _dates_compatible(kalshi_close_date, candidate.close_date):
            continue   # Date mismatch — hard reject regardless of text score

        if score > best_score:
            best_score = score
            best_candidate = candidate
            best_tsr = tsr
            best_pr = pr

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

    # Below low-confidence floor: silent reject
    if best_score < config.FUZZY_LOW_CONF_MIN:
        return None

    # Confident match (>= FUZZY_MATCH_THRESHOLD)
    match = Match(
        kalshi_title=kalshi_title,
        kalshi_ticker=kalshi_ticker,
        kalshi_close_date=kalshi_close_date,
        candidate=best_candidate,
        score=best_score,
        token_sort_ratio=best_tsr,
        partial_ratio=best_pr,
    )

    # Persist to cache
    key = _cache_key(kalshi_ticker, best_candidate.source)
    with _cache_lock:
        _cache[key] = asdict(match)
        _save_cache()

    return match


def find_matches_multi_source(
    kalshi_ticker: str,
    kalshi_title: str,
    kalshi_close_date: Optional[datetime],
    manifold_candidates: List[Candidate],
    predictit_candidates: List[Candidate],
) -> Dict[str, Optional[Match]]:
    """
    Run find_match against both Manifold and PredictIt candidate lists.

    Returns:
        {"manifold": Match|None, "predictit": Match|None}
    """
    manifold_match = find_match(
        kalshi_ticker, kalshi_title, kalshi_close_date, manifold_candidates
    )
    predictit_match = find_match(
        kalshi_ticker, kalshi_title, kalshi_close_date, predictit_candidates
    )
    return {"manifold": manifold_match, "predictit": predictit_match}


def invalidate_cache() -> None:
    """Force-clear the in-memory and on-disk cache."""
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = {}
        _cache_loaded_at = None
        path = config.MATCH_CACHE_FILE
        if os.path.exists(path):
            os.remove(path)
