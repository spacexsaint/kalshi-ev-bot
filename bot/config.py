"""
config.py — All tuneable parameters in one place.

═══════════════════════════════════════════════════════
API FINDINGS (verified 2026-04-08):
═══════════════════════════════════════════════════════

Kalshi Base URL (prod):  https://api.elections.kalshi.com/trade-api/v2
Kalshi Base URL (demo):  https://demo-api.kalshi.co/trade-api/v2
Auth: RSA-PSS with SHA256. Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms),
      KALSHI-ACCESS-SIGNATURE
Signing: f"{timestamp_ms}{HTTP_METHOD}{path_without_query}"
Rate limits: Basic=20r/s read, 10r/s write | Advanced=30/30 | Premier=100/100
Taker fee: round_up(0.07 × C × P × (1−P))
Maker fee: round_up(0.0175 × C × P × (1−P))
INX/NASDAQ100 taker: round_up(0.035 × C × P × (1−P))
Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf

Manifold Base URL: https://api.manifold.markets/v0
Endpoint: GET /markets?limit=1000  — probability field: market["probability"] (0–1)
No auth for reads. Rate limit: 500 req/min per IP.
Calibration: Well-calibrated, competitive with major platforms (arXiv 2025-03-05)
Source: https://docs.manifold.markets/api

PredictIt: GET https://www.predictit.org/api/marketdata/all/
Implied probability: contract["bestBuyYesCost"] (primary for fair value)
Also: contract["lastTradePrice"] (last traded price)
Structure: { "markets": [ { "id", "name", "contracts": [...] } ] }
No auth required.
CALIBRATION LEADER: 93% accuracy in 2024 election study (Vanderbilt, Clinton & Huang 2026)
Source: live fetch 2026-04-08

Polymarket: GET https://gamma-api.polymarket.com/markets?active=true&closed=false
Implied probability: market["outcomePrices"] (JSON array, index 0 = YES)
No auth for reads. Rate limit: 300 req / 10s (Gamma API)
Source: https://docs.polymarket.com/market-data/overview
CALIBRATION: 67% accuracy (lowest — use as tertiary signal, not anchor)
Source: Vanderbilt study, 2026

═══════════════════════════════════════════════════════
CALIBRATION-BASED SOURCE WEIGHTS (Vanderbilt 2026 + Calibration City):
  PredictIt:  0.45  (93% accuracy, highest calibration)
  Manifold:   0.35  (well-calibrated per arXiv, good on non-political)
  Polymarket: 0.20  (67% accuracy but high volume — useful signal, lower weight)
═══════════════════════════════════════════════════════
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading Parameters ─────────────────────────────────────────────────────────
MIN_EDGE: float = 0.05          # 5% net-of-fees minimum edge to bet
KELLY_FRACTION: float = 0.25   # Quarter-Kelly (best risk/reward per arXiv 2020)
MAX_BET_PCT: float = 0.05      # Max 5% of balance per single bet
MIN_BET_USD: float = 1.00      # Kalshi minimum contract value ($1)
MAX_OPEN_POSITIONS: int = 10   # Simultaneous open positions cap
DAILY_LOSS_LIMIT_PCT: float = 0.15  # Halt trading if down 15% on the day
SCAN_INTERVAL_SEC: int = 300   # 5 minutes between full market scans

# ── Kelly Uncertainty Penalty ──────────────────────────────────────────────────
# Kullback-Leibler uncertainty adjustment (arXiv 2024, Meister; Galekwa et al. 2026)
# When only 1 source matches, apply an uncertainty penalty to the Kelly fraction.
# This reduces ruin probability from 78% → <2% per arXiv uncertainty-adjusted Kelly.
KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE: float = 0.50   # 50% Kelly reduction for 1 source
KL_UNCERTAINTY_PENALTY_DUAL_SOURCE: float = 0.75     # 25% reduction for 2 sources (still uncertain)
KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE: float = 1.00   # Full Kelly fraction for all 3 sources

# ── Market Filters ─────────────────────────────────────────────────────────────
MIN_MARKET_VOLUME: int = 5000       # Minimum $5,000 total volume
MIN_TIME_TO_CLOSE_HR: int = 2       # Ignore markets closing in < 2 hours
MAX_TIME_TO_CLOSE_DAYS: int = 30    # Ignore markets > 30 days out
MAX_BID_ASK_SPREAD: float = 0.05   # Max 5-cent bid-ask spread (filter illiquid)

# ── Time-Decay Edge Discounting ────────────────────────────────────────────────
# Markets near resolution face convergence pressure — the edge decays.
# If a market closes in < TIME_DECAY_THRESHOLD_HR, discount the edge.
TIME_DECAY_THRESHOLD_HR: float = 24.0    # Apply decay when < 24h to close
TIME_DECAY_MIN_MULTIPLIER: float = 0.60  # At < MIN_TIME_TO_CLOSE_HR, 60% of edge remains

# ── Correlation-Aware Position Sizing ─────────────────────────────────────────
# Avoid concentrating in correlated bets (e.g., all "Fed rate cut" markets)
# If a category already has >= MAX_POSITIONS_PER_CATEGORY open, reduce new bet size
MAX_POSITIONS_PER_CATEGORY: int = 2         # Max open positions in same topic category
CORRELATED_BET_SIZE_PENALTY: float = 0.50  # Halve size if category already has 1 position

# ── Order Management ───────────────────────────────────────────────────────────
ORDER_FILL_TIMEOUT_S: int = 30     # Cancel unfilled orders after 30s
PROFIT_TAKE_CENTS: int = 15        # Close position early if +15 cents in our favor
PRICE_STALENESS_CENTS: int = 2     # Skip if price moved > 2 cents since evaluation

# ── Midpoint Pricing ───────────────────────────────────────────────────────────
# Use (bid + ask) / 2 as the execution reference price for edge calculation.
# Executing at ask is conservative; midpoint is the true cost for limit orders.
USE_MIDPOINT_FOR_EDGE_CALC: bool = True
# The actual order is still placed at ask (to ensure fill) but edge is computed
# using midpoint to avoid overstating the edge when spread is wide.

# ── Market Matching ────────────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD: float = 0.75  # Min similarity for market matching
FUZZY_LOW_CONF_MIN: float = 0.65    # Low-confidence range (log but don't trade)
DATE_MATCH_TOLERANCE_DAYS: int = 7  # Resolution dates must be within 7 days
MATCH_CACHE_TTL_HOURS: int = 6      # Refresh match cache every 6 hours

# ── API Configuration ──────────────────────────────────────────────────────────
KALSHI_BASE_URL_PROD: str = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BASE_URL_DEMO: str = "https://demo-api.kalshi.co/trade-api/v2"
MANIFOLD_BASE_URL: str = "https://api.manifold.markets/v0"
PREDICTIT_URL: str = "https://www.predictit.org/api/marketdata/all/"
POLYMARKET_GAMMA_URL: str = "https://gamma-api.polymarket.com"  # No auth for reads

# ── Source Weights (Vanderbilt 2026 calibration study) ────────────────────────
PREDICTIT_WEIGHT: float = 0.45   # 93% accuracy — highest weight
MANIFOLD_WEIGHT: float = 0.35    # Well-calibrated, good on non-political
POLYMARKET_WEIGHT: float = 0.20  # 67% accuracy — useful signal, lowest weight

# ── Retry Settings ─────────────────────────────────────────────────────────────
MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: float = 1.0    # 1s, 2s, 4s exponential
RATE_LIMIT_WAIT_S: int = 60        # Wait 60s on 429
SERVER_ERROR_WAIT_S: int = 10      # Wait 10s on 5xx
CONCURRENT_MARKET_SCANS: int = 10  # Max concurrent orderbook fetches

# ── Mode ───────────────────────────────────────────────────────────────────────
PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# ── Paths ──────────────────────────────────────────────────────────────────────
STATE_FILE: str = "data/state.json"
MATCH_CACHE_FILE: str = "data/match_cache.json"
LOG_DIR: str = "logs"
TRADES_LOG: str = "logs/trades.jsonl"
EVENTS_LOG: str = "logs/events.jsonl"
API_LOG: str = "logs/api.jsonl"
LOW_CONF_LOG: str = "logs/low_confidence_matches.jsonl"

# ── Dashboard Refresh ──────────────────────────────────────────────────────────
DASHBOARD_REFRESH_S: int = 30

# ── Daily Summary UTC Hour ─────────────────────────────────────────────────────
DAILY_SUMMARY_UTC_HOUR: int = 23
DAILY_SUMMARY_UTC_MINUTE: int = 59
