"""
fair_value.py — Aggregate fair-value probability from PredictIt + Manifold + Polymarket.

SOURCES (all free, no auth for reads):
  PredictIt:  GET https://www.predictit.org/api/marketdata/all/
              probability = contract["bestBuyYesCost"]
              Calibration: 93% accuracy (Vanderbilt 2026) — BEST on politics
  Manifold:   GET https://api.manifold.markets/v0/markets?limit=1000&filter=open
              probability = market["probability"]
              Calibration: well-calibrated (arXiv 2025) — BEST on tech/science/general
  Polymarket: GET https://gamma-api.polymarket.com/markets?active=true&closed=false
              probability = market["outcomePrices"][0] (YES price)
              Calibration: 67% accuracy (Vanderbilt 2026) — BEST on sports/crypto

AGGREGATION:
  Uses category-specific weights from config.CATEGORY_SOURCE_WEIGHTS when category
  is recognised (e.g., "election" → PredictIt 60%, "btc" → Polymarket 65%).
  Falls back to global weights for uncategorised markets.
  Renormalises weights for whichever sources are actually available.

CONFIDENCE:
  "triple" = all 3 sources matched
  "dual"   = 2 sources matched
  "single" = 1 source matched
  None returned if 0 sources matched (never trade blind)
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

import aiohttp
import numpy as np

from bot import config
from bot import logger as bot_logger
from bot.market_matcher import Candidate, Match, find_match

_log = logging.getLogger(__name__)


@dataclass
class FairValue:
    probability: float
    confidence: Literal["triple", "dual", "single", "none"]
    sources: List[str]
    source_count: int
    category: str = "uncategorized"
    predictit_prob: Optional[float] = None
    manifold_prob: Optional[float] = None
    polymarket_prob: Optional[float] = None
    predictit_match_score: Optional[float] = None
    manifold_match_score: Optional[float] = None
    polymarket_match_score: Optional[float] = None
    source_disagreement_mult: float = 1.0  # 1.0 = no penalty; reduced when sources diverge


# ── Module-level caches ────────────────────────────────────────────────────────
_manifold_cache: List[Candidate] = []
_predictit_cache: List[Candidate] = []
_polymarket_cache: List[Candidate] = []
_fetched_at: Optional[float] = None
_CACHE_TTL_S: float = 290.0
_refresh_lock: Optional[asyncio.Lock] = None  # Lazy-init to avoid event loop issues

# Track consecutive fetch failures per source.
# If a source fails N times in a row, log prominently so operator knows
# we may be trading on fewer sources than expected.
_source_fail_counts: Dict[str, int] = {"predictit": 0, "manifold": 0, "polymarket": 0}
_SOURCE_FAIL_ALERT_THRESHOLD: int = 3  # Log WARNING after this many consecutive failures


def _get_refresh_lock() -> asyncio.Lock:
    """Lazy-initialise the asyncio.Lock (must be created inside a running event loop)."""
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _track_source_health(source_name: str, candidates: List[Candidate]) -> None:
    """
    Track consecutive fetch failures per source.

    If a source returns 0 candidates for _SOURCE_FAIL_ALERT_THRESHOLD consecutive
    refreshes, log a prominent warning. The operator needs to know we may be
    trading on fewer sources than expected (degraded confidence).
    """
    if candidates:
        if _source_fail_counts.get(source_name, 0) >= _SOURCE_FAIL_ALERT_THRESHOLD:
            _log.info("Source %s recovered after %d consecutive failures.", source_name, _source_fail_counts[source_name])
        _source_fail_counts[source_name] = 0
    else:
        _source_fail_counts[source_name] = _source_fail_counts.get(source_name, 0) + 1
        count = _source_fail_counts[source_name]
        if count >= _SOURCE_FAIL_ALERT_THRESHOLD:
            _log.warning(
                "SOURCE DOWN: %s has returned 0 markets for %d consecutive refreshes. "
                "Trading may be using fewer sources than expected.",
                source_name, count,
            )
            bot_logger.log_event(
                "source_failure",
                f"{source_name} down for {count} consecutive refreshes",
                extra={"source": source_name, "consecutive_failures": count},
                severity="warning",
            )


# ── HTTP helper with retry ────────────────────────────────────────────────────
async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
    max_retries: int = 2,
) -> dict | list | None:
    """
    GET with exponential backoff retry (2 retries: 1s, 2s).
    A single network blip previously caused an entire source to be silently
    dropped for the full scan cycle. Retrying recovers transient failures
    without significantly increasing latency (external sources are non-critical path).
    """
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                latency_ms = (time.monotonic() - t0) * 1000
                bot_logger.log_api_call(
                    method="GET", endpoint=url,
                    status_code=resp.status,
                    latency_ms=latency_ms,
                )
                if resp.status == 200:
                    return await resp.json(content_type=None)
                if resp.status == 429 and attempt < max_retries:
                    await asyncio.sleep(2.0 ** attempt)
                    continue
                _log.warning("HTTP %s from %s (attempt %d)", resp.status, url, attempt + 1)
                if attempt < max_retries and resp.status >= 500:
                    await asyncio.sleep(1.0 * (2 ** attempt))
                    continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            bot_logger.log_api_call(
                method="GET", endpoint=url, status_code=0,
                latency_ms=latency_ms, error=str(exc),
            )
            if attempt < max_retries:
                wait = 1.0 * (2 ** attempt)
                _log.warning("Source fetch failed (attempt %d/%d): %s — retrying in %.0fs",
                             attempt + 1, max_retries + 1, exc, wait)
                await asyncio.sleep(wait)
                continue
            _log.error("Source fetch exhausted retries for %s: %s", url, exc)
            return None
    return None


# ── SOURCE 1: PredictIt ────────────────────────────────────────────────────────
async def _fetch_predictit(session: aiohttp.ClientSession) -> List[Candidate]:
    data = await _get_json(session, config.PREDICTIT_URL)
    if not data or not isinstance(data, dict):
        return []
    candidates: List[Candidate] = []
    for market in data.get("markets", []):
        if market.get("status", "").lower() != "open":
            continue
        contracts = market.get("contracts", [])
        for contract in contracts:
            prob = contract.get("bestBuyYesCost") or contract.get("lastTradePrice")
            if prob is None:
                continue
            try:
                prob = float(prob)
            except (TypeError, ValueError):
                continue
            if not (0.0 < prob < 1.0):
                continue
            title = (
                market.get("name", "") if len(contracts) == 1
                else f"{market.get('name', '')} — {contract.get('name', '')}"
            )
            close_dt: Optional[datetime] = None
            end = contract.get("dateEnd")
            if end and end != "NA":
                try:
                    close_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
            candidates.append(Candidate(
                title=title, probability=prob, close_date=close_dt,
                source="predictit", market_id=f"{market.get('id')}:{contract.get('id')}",
            ))
    _log.info("Fetched %d PredictIt markets", len(candidates))
    return candidates


# ── SOURCE 2: Manifold ─────────────────────────────────────────────────────────
async def _fetch_manifold(session: aiohttp.ClientSession) -> List[Candidate]:
    candidates: List[Candidate] = []
    cursor: Optional[str] = None
    while True:
        params: dict = {"limit": 1000, "filter": "open", "contractType": "BINARY",
                        "sort": "created-time", "order": "desc"}
        if cursor:
            params["before"] = cursor
        data = await _get_json(session, f"{config.MANIFOLD_BASE_URL}/markets", params=params)
        if not data or not isinstance(data, list):
            break
        for m in data:
            prob = m.get("probability")
            if prob is None or m.get("isResolved"):
                continue
            try:
                prob = float(prob)
            except (TypeError, ValueError):
                continue
            # Clip to (0, 1) — raw 0.0 or 1.0 would dominate std calculation
            # and produce extreme disagreement penalties. PredictIt and Polymarket
            # already filter strict (0,1); Manifold must match.
            if not (0.0 < prob < 1.0):
                continue
            close_dt: Optional[datetime] = None
            ct = m.get("closeTime")
            if ct:
                try:
                    close_dt = datetime.fromtimestamp(ct / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    pass
            candidates.append(Candidate(
                title=m.get("question", ""), probability=float(prob),
                close_date=close_dt, source="manifold", market_id=m.get("id", ""),
            ))
        if len(data) < 1000:
            break
        cursor = data[-1].get("id")
    _log.info("Fetched %d Manifold markets", len(candidates))
    return candidates


# ── SOURCE 3: Polymarket ───────────────────────────────────────────────────────
async def _fetch_polymarket(session: aiohttp.ClientSession) -> List[Candidate]:
    candidates: List[Candidate] = []
    offset = 0
    while True:
        data = await _get_json(
            session,
            f"{config.POLYMARKET_GAMMA_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 100,
                    "offset": offset, "order": "volume_24hr", "ascending": "false"},
        )
        if not data or not isinstance(data, list):
            break
        for market in data:
            outcomes_raw = market.get("outcomes", "")
            try:
                outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            except Exception:
                outcomes = []
            if not outcomes or len(outcomes) != 2:
                continue
            prices_raw = market.get("outcomePrices", "")
            try:
                prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                yes_price = float(prices[0]) if prices else None
            except Exception:
                yes_price = None
            if yes_price is None:
                ltp = market.get("lastTradePrice")
                try:
                    yes_price = float(ltp) if ltp is not None else None
                except (TypeError, ValueError):
                    pass
            if yes_price is None or not (0.0 < yes_price < 1.0):
                continue
            close_dt: Optional[datetime] = None
            for df in ["endDate", "endDateIso"]:
                if market.get(df):
                    try:
                        close_dt = datetime.fromisoformat(str(market[df]).replace("Z", "+00:00"))
                        break
                    except (ValueError, AttributeError):
                        pass
            candidates.append(Candidate(
                title=market.get("question", market.get("title", "")),
                probability=yes_price, close_date=close_dt,
                source="polymarket", market_id=str(market.get("id", "")),
            ))
        if len(data) < 100 or offset >= 2000:
            break
        offset += 100
    _log.info("Fetched %d Polymarket markets", len(candidates))
    return candidates


# ── Cache management ───────────────────────────────────────────────────────────
async def refresh_all_sources(session: aiohttp.ClientSession) -> None:
    """Refresh all source caches. Lock prevents concurrent refresh stampede."""
    global _predictit_cache, _manifold_cache, _polymarket_cache, _fetched_at
    lock = _get_refresh_lock()
    async with lock:
        # Re-check after acquiring lock (another coroutine may have refreshed)
        if _caches_valid():
            return
        predictit, manifold, polymarket = await asyncio.gather(
            _fetch_predictit(session),
            _fetch_manifold(session),
            _fetch_polymarket(session),
        )
        _predictit_cache = predictit
        _manifold_cache = manifold
        _polymarket_cache = polymarket
        _fetched_at = time.monotonic()

        # Track consecutive failures per source.
        # Alert operator if a source is down for multiple consecutive cycles.
        _track_source_health("predictit", predictit)
        _track_source_health("manifold", manifold)
        _track_source_health("polymarket", polymarket)


def _caches_valid() -> bool:
    return _fetched_at is not None and (time.monotonic() - _fetched_at) < _CACHE_TTL_S


# ── Category-aware aggregation ─────────────────────────────────────────────────
def _get_weights_for_category(category: str) -> Dict[str, float]:
    """
    Return source weights for a given market category.

    Uses config.CATEGORY_SOURCE_WEIGHTS if category is recognised,
    otherwise falls back to global weights.
    """
    cat_weights = config.CATEGORY_SOURCE_WEIGHTS.get(category)
    if cat_weights:
        return cat_weights
    return {
        "predictit": config.PREDICTIT_WEIGHT,
        "manifold": config.MANIFOLD_WEIGHT,
        "polymarket": config.POLYMARKET_WEIGHT,
    }


def _aggregate_probabilities(
    predictit_prob: Optional[float],
    manifold_prob: Optional[float],
    polymarket_prob: Optional[float],
    category: str = "uncategorized",
) -> tuple[float, int, List[str], float]:
    """
    Weighted aggregation using category-specific calibration weights.

    Weights are renormalised to sum to 1.0 over whichever sources matched.
    Returns: (probability, source_count, source_names, source_disagreement_mult)

    Source disagreement multiplier:
      When multiple sources give divergent probabilities, our confidence in
      the weighted average is lower. We penalise the Kelly fraction to reflect
      this. Population std of contributing source probabilities:
        std > 0.20  → mult = 0.50  (severe disagreement, halve Kelly)
        std > 0.10  → mult = 0.75  (moderate disagreement)
        else        → mult = 1.00  (sources agree, no penalty)
    """
    available: Dict[str, float] = {}
    if predictit_prob is not None:
        available["predictit"] = predictit_prob
    if manifold_prob is not None:
        available["manifold"] = manifold_prob
    if polymarket_prob is not None:
        available["polymarket"] = polymarket_prob

    if not available:
        return 0.0, 0, [], 1.0

    weight_map = _get_weights_for_category(category)

    # Renormalise to only present sources
    total_w = sum(weight_map.get(src, 0.0) for src in available)
    if total_w <= 0:
        # Equal weighting fallback
        prob = sum(available.values()) / len(available)
        disagreement_mult = _compute_disagreement_mult(list(available.values()))
        return prob, len(available), list(available.keys()), disagreement_mult

    weighted_prob = sum(
        prob * weight_map.get(src, 0.0) / total_w
        for src, prob in available.items()
    )

    disagreement_mult = _compute_disagreement_mult(list(available.values()))
    return weighted_prob, len(available), list(available.keys()), disagreement_mult


def _compute_disagreement_mult(source_probs: List[float]) -> float:
    """
    Compute source disagreement multiplier from individual source probabilities.

    Uses population std (ddof=0) of the contributing source probabilities.
    Only applies when >=2 sources present; single source gets no penalty.
    """
    if len(source_probs) < 2:
        return 1.0
    std = float(np.std(source_probs, ddof=0))
    if std > 0.20:
        return 0.50
    if std > 0.10:
        return 0.75
    return 1.0


# ── Main entry point ───────────────────────────────────────────────────────────
async def get_fair_value(
    kalshi_ticker: str,
    kalshi_title: str,
    kalshi_close_date: Optional[datetime],
    session: aiohttp.ClientSession,
    category: str = "uncategorized",
) -> Optional[FairValue]:
    """
    Get calibration-weighted fair probability for a Kalshi market.

    Uses category-specific weights so that:
    - Elections use PredictIt-dominant weighting
    - Sports/crypto use Polymarket-dominant weighting
    - Tech/science use Manifold-dominant weighting
    - Returns None if no source matches (never trade blind)
    """
    if not _caches_valid():
        await refresh_all_sources(session)

    predictit_match: Optional[Match] = find_match(
        kalshi_ticker, kalshi_title, kalshi_close_date, _predictit_cache)
    manifold_match: Optional[Match] = find_match(
        kalshi_ticker, kalshi_title, kalshi_close_date, _manifold_cache)
    polymarket_match: Optional[Match] = find_match(
        kalshi_ticker, kalshi_title, kalshi_close_date, _polymarket_cache)

    predictit_prob = predictit_match.candidate.probability if predictit_match else None
    manifold_prob = manifold_match.candidate.probability if manifold_match else None
    polymarket_prob = polymarket_match.candidate.probability if polymarket_match else None

    aggregated, source_count, source_names, disagreement_mult = _aggregate_probabilities(
        predictit_prob, manifold_prob, polymarket_prob, category
    )

    if source_count == 0:
        return None

    confidence: Literal["triple", "dual", "single", "none"]
    if source_count >= 3:
        confidence = "triple"
    elif source_count == 2:
        confidence = "dual"
    else:
        confidence = "single"
        bot_logger.log_event(
            "single_source",
            f"Only {source_names[0]} matched for {kalshi_ticker}",
            extra={"ticker": kalshi_ticker, "source": source_names[0],
                   "prob": aggregated, "category": category},
            severity="warning",
        )

    return FairValue(
        probability=aggregated,
        confidence=confidence,
        sources=source_names,
        source_count=source_count,
        category=category,
        predictit_prob=predictit_prob,
        manifold_prob=manifold_prob,
        polymarket_prob=polymarket_prob,
        predictit_match_score=predictit_match.score if predictit_match else None,
        manifold_match_score=manifold_match.score if manifold_match else None,
        polymarket_match_score=polymarket_match.score if polymarket_match else None,
        source_disagreement_mult=disagreement_mult,
    )


async def test_connectivity(session: aiohttp.ClientSession) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    d1 = await _get_json(session, f"{config.MANIFOLD_BASE_URL}/markets", {"limit": 1})
    results["manifold"] = isinstance(d1, list)
    d2 = await _get_json(session, config.PREDICTIT_URL)
    results["predictit"] = isinstance(d2, dict) and "markets" in d2
    d3 = await _get_json(session, f"{config.POLYMARKET_GAMMA_URL}/markets",
                         {"active": "true", "closed": "false", "limit": 1})
    results["polymarket"] = isinstance(d3, list)
    return results
