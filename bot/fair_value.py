"""
fair_value.py — Aggregate fair-value probability from Manifold + PredictIt.

SOURCE 1 — Manifold Markets:
  Endpoint: GET https://api.manifold.markets/v0/markets?limit=1000&filter=open&contractType=BINARY
  Field: market["probability"] (0–1, no conversion needed)
  No auth required. Rate limit: 500 req/min per IP.
  Source: https://docs.manifold.markets/api

SOURCE 2 — PredictIt:
  Endpoint: GET https://www.predictit.org/api/marketdata/all/
  Implied probability = contract["bestBuyYesCost"] (best available Yes buy price)
  Fallback: contract["lastTradePrice"]
  No auth required.
  Source: live fetch 2026-04-08

Aggregation:
  dual   (both match): w = 0.6 × manifold + 0.4 × predictit
  single (one match):  w = that source's probability
  none   (no match):   w = None (DO NOT TRADE)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Tuple

import aiohttp

from bot import config
from bot import logger as bot_logger
from bot.market_matcher import Candidate, Match, find_match

_log = logging.getLogger(__name__)


@dataclass
class FairValue:
    probability: float                         # 0–1 aggregated fair probability
    confidence: Literal["dual", "single", "none"]
    sources: List[str]                         # e.g. ["manifold", "predictit"]
    manifold_prob: Optional[float] = None
    predictit_prob: Optional[float] = None
    manifold_match_score: Optional[float] = None
    predictit_match_score: Optional[float] = None


# ── Module-level market cache (refreshed each scan cycle) ─────────────────────

_manifold_cache: List[Candidate] = []
_predictit_cache: List[Candidate] = []
_manifold_fetched_at: Optional[float] = None
_predictit_fetched_at: Optional[float] = None
_CACHE_TTL_S: float = 290.0   # Slightly less than SCAN_INTERVAL_SEC


# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict | list | None:
    t0 = time.monotonic()
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            latency_ms = (time.monotonic() - t0) * 1000
            bot_logger.log_api_call(
                method="GET",
                endpoint=url,
                status_code=resp.status,
                latency_ms=latency_ms,
            )
            if resp.status == 200:
                return await resp.json(content_type=None)
            _log.warning("HTTP %s from %s", resp.status, url)
            return None
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        bot_logger.log_api_call(
            method="GET",
            endpoint=url,
            status_code=0,
            latency_ms=latency_ms,
            error=str(exc),
        )
        _log.error("Request failed for %s: %s", url, exc)
        return None


# ── Manifold fetching ──────────────────────────────────────────────────────────

async def _fetch_manifold_markets(session: aiohttp.ClientSession) -> List[Candidate]:
    """Fetch all open binary markets from Manifold."""
    candidates: List[Candidate] = []
    cursor: Optional[str] = None

    while True:
        params: dict = {
            "limit": 1000,
            "filter": "open",
            "contractType": "BINARY",
            "sort": "created-time",
            "order": "desc",
        }
        if cursor:
            params["before"] = cursor

        data = await _get_json(
            session,
            f"{config.MANIFOLD_BASE_URL}/markets",
            params=params,
        )
        if not data or not isinstance(data, list):
            break

        for market in data:
            prob = market.get("probability")
            if prob is None:
                continue
            if market.get("isResolved"):
                continue

            close_time = market.get("closeTime")
            close_dt: Optional[datetime] = None
            if close_time:
                try:
                    close_dt = datetime.fromtimestamp(close_time / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    pass

            candidates.append(
                Candidate(
                    title=market.get("question", ""),
                    probability=float(prob),
                    close_date=close_dt,
                    source="manifold",
                    market_id=market.get("id", ""),
                )
            )

        # Manifold uses ID-based pagination; if fewer than limit returned, we're done
        if len(data) < 1000:
            break
        cursor = data[-1].get("id")

    _log.info("Fetched %d Manifold binary markets", len(candidates))
    return candidates


# ── PredictIt fetching ─────────────────────────────────────────────────────────

async def _fetch_predictit_markets(session: aiohttp.ClientSession) -> List[Candidate]:
    """Fetch all open binary markets from PredictIt."""
    data = await _get_json(session, config.PREDICTIT_URL)
    if not data or not isinstance(data, dict):
        return []

    candidates: List[Candidate] = []
    markets = data.get("markets", [])

    for market in markets:
        if market.get("status", "").lower() != "open":
            continue

        contracts = market.get("contracts", [])

        # Only process binary markets (exactly one YES/NO contract pair)
        # PredictIt binary: one contract with yes/no
        # Multi-contract markets: multiple options (skip or use YES contract only)
        for contract in contracts:
            # Use bestBuyYesCost as primary; fallback to lastTradePrice
            prob = contract.get("bestBuyYesCost") or contract.get("lastTradePrice")
            if prob is None:
                continue
            try:
                prob = float(prob)
            except (TypeError, ValueError):
                continue

            if not (0.0 < prob < 1.0):
                continue

            # Build a descriptive title: market name + contract name if multi-contract
            if len(contracts) == 1:
                title = market.get("name", "")
            else:
                market_name = market.get("name", "")
                contract_name = contract.get("name", "")
                title = f"{market_name} — {contract_name}"

            # PredictIt doesn't return explicit close dates; use None
            end_date = contract.get("dateEnd")
            close_dt: Optional[datetime] = None
            if end_date and end_date != "NA":
                try:
                    close_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            candidates.append(
                Candidate(
                    title=title,
                    probability=prob,
                    close_date=close_dt,
                    source="predictit",
                    market_id=str(market.get("id", "")) + ":" + str(contract.get("id", "")),
                )
            )

    _log.info("Fetched %d PredictIt contract markets", len(candidates))
    return candidates


# ── Cache refresh ──────────────────────────────────────────────────────────────

async def refresh_all_sources(session: aiohttp.ClientSession) -> None:
    """Fetch fresh data from both Manifold and PredictIt and update module caches."""
    global _manifold_cache, _predictit_cache, _manifold_fetched_at, _predictit_fetched_at

    manifold, predictit = await asyncio.gather(
        _fetch_manifold_markets(session),
        _fetch_predictit_markets(session),
    )

    _manifold_cache = manifold
    _predictit_cache = predictit
    now = time.monotonic()
    _manifold_fetched_at = now
    _predictit_fetched_at = now


def _caches_valid() -> bool:
    if _manifold_fetched_at is None or _predictit_fetched_at is None:
        return False
    age = time.monotonic() - min(_manifold_fetched_at, _predictit_fetched_at)
    return age < _CACHE_TTL_S


# ── Main entry-point ───────────────────────────────────────────────────────────

async def get_fair_value(
    kalshi_ticker: str,
    kalshi_title: str,
    kalshi_close_date: Optional[datetime],
    session: aiohttp.ClientSession,
) -> Optional[FairValue]:
    """
    Get aggregated fair-value probability for a Kalshi market.

    Fetches from Manifold and PredictIt (caches results within a scan cycle),
    fuzzy-matches titles, and aggregates probabilities.

    Returns:
        FairValue with probability and confidence, or None if no match found.
    """
    # Refresh caches if stale
    if not _caches_valid():
        await refresh_all_sources(session)

    manifold_candidates = _manifold_cache
    predictit_candidates = _predictit_cache

    # Match against Manifold
    manifold_match: Optional[Match] = find_match(
        kalshi_ticker=kalshi_ticker,
        kalshi_title=kalshi_title,
        kalshi_close_date=kalshi_close_date,
        candidates=manifold_candidates,
    )

    # Match against PredictIt
    predictit_match: Optional[Match] = find_match(
        kalshi_ticker=kalshi_ticker,
        kalshi_title=kalshi_title,
        kalshi_close_date=kalshi_close_date,
        candidates=predictit_candidates,
    )

    manifold_prob = manifold_match.candidate.probability if manifold_match else None
    predictit_prob = predictit_match.candidate.probability if predictit_match else None

    # Aggregation logic
    if manifold_prob is not None and predictit_prob is not None:
        combined = config.MANIFOLD_WEIGHT * manifold_prob + config.PREDICTIT_WEIGHT * predictit_prob
        return FairValue(
            probability=combined,
            confidence="dual",
            sources=["manifold", "predictit"],
            manifold_prob=manifold_prob,
            predictit_prob=predictit_prob,
            manifold_match_score=manifold_match.score if manifold_match else None,
            predictit_match_score=predictit_match.score if predictit_match else None,
        )

    if manifold_prob is not None:
        bot_logger.log_event(
            "single_source",
            f"Only Manifold matched for {kalshi_ticker} ({kalshi_title[:60]})",
            extra={"ticker": kalshi_ticker, "source": "manifold", "prob": manifold_prob},
        )
        return FairValue(
            probability=manifold_prob,
            confidence="single",
            sources=["manifold"],
            manifold_prob=manifold_prob,
            manifold_match_score=manifold_match.score if manifold_match else None,
        )

    if predictit_prob is not None:
        bot_logger.log_event(
            "single_source",
            f"Only PredictIt matched for {kalshi_ticker} ({kalshi_title[:60]})",
            extra={"ticker": kalshi_ticker, "source": "predictit", "prob": predictit_prob},
        )
        return FairValue(
            probability=predictit_prob,
            confidence="single",
            sources=["predictit"],
            predictit_prob=predictit_prob,
            predictit_match_score=predictit_match.score if predictit_match else None,
        )

    # No match — do not trade
    return None


async def test_connectivity(session: aiohttp.ClientSession) -> Dict[str, bool]:
    """Lightweight connectivity check for startup validation."""
    results: Dict[str, bool] = {}

    # Manifold: fetch just 1 market
    data = await _get_json(
        session,
        f"{config.MANIFOLD_BASE_URL}/markets",
        params={"limit": 1},
    )
    results["manifold"] = isinstance(data, list)

    # PredictIt: check top-level structure
    data2 = await _get_json(session, config.PREDICTIT_URL)
    results["predictit"] = isinstance(data2, dict) and "markets" in data2

    return results
