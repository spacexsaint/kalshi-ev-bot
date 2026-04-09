"""
config.py — All tuneable parameters in one place.

═══════════════════════════════════════════════════════
API VERIFIED 2026-04-09:
  Kalshi prod:  https://api.elections.kalshi.com/trade-api/v2
  Kalshi demo:  https://demo-api.kalshi.co/trade-api/v2
  Auth: RSA-PSS SHA-256, headers KALSHI-ACCESS-KEY/TIMESTAMP/SIGNATURE
  Taker fee: ceil(0.07 × C × P × (1−P))   src: kalshi.com/docs/kalshi-fee-schedule.pdf
  INX/NASDAQ100 taker: ceil(0.035 × C × P × (1−P))

  Manifold:   GET https://api.manifold.markets/v0/markets?limit=1000&filter=open
              field: market["probability"]  |  no auth  |  500 req/min
  PredictIt:  GET https://www.predictit.org/api/marketdata/all/
              field: contract["bestBuyYesCost"]  |  no auth
  Polymarket: GET https://gamma-api.polymarket.com/markets?active=true&closed=false
              field: market["outcomePrices"][0]  |  no auth  |  300 req/10s
  Metaculus:  GET https://www.metaculus.com/api2/questions/?status=open&type=forecast
              field: question["community_prediction"]["full"]["q2"]  |  no auth
              NOTE: Cloudflare-protected, fetch via browser_task if direct fails.

CALIBRATION WEIGHTS (Vanderbilt 2026 + Calibration City):
  PredictIt  = 0.45  (93% accuracy overall, best on politics)
  Manifold   = 0.35  (well-calibrated, best on tech/science/general)
  Polymarket = 0.20  (67% accuracy, high volume, best on sports/crypto)
  Metaculus  = used when matched, blended into weighted avg (science/tech boost)
═══════════════════════════════════════════════════════
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading Parameters ─────────────────────────────────────────────────────────
MIN_EDGE: float = 0.05          # 5% net-of-fees minimum edge
KELLY_FRACTION: float = 0.25   # Quarter-Kelly (arXiv 2020: optimal risk/return tradeoff)
MAX_BET_PCT: float = 0.05      # Max 5% of balance per single bet
MIN_BET_USD: float = 1.00      # Kalshi minimum ($1)
MAX_OPEN_POSITIONS: int = 10
DAILY_LOSS_LIMIT_PCT: float = 0.15   # Halt at 15% daily loss
SCAN_INTERVAL_SEC: int = 300

# ── Position Management ────────────────────────────────────────────────────────
PROFIT_TAKE_CENTS: int = 15    # Close early if bid >= entry + 15c
STOP_LOSS_CENTS: int = 20      # Close early if bid <= entry - 20c (new)
PRICE_STALENESS_CENTS: int = 2
ORDER_FILL_TIMEOUT_S: int = 30

# ── KL Uncertainty Penalties (Meister arXiv 2024, Galekwa IEEE 2026) ──────────
# Reduces ruin probability from 78% → <2% for single-source bets
KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE: float = 0.50
KL_UNCERTAINTY_PENALTY_DUAL_SOURCE: float = 0.75
KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE: float = 1.00
KL_UNCERTAINTY_PENALTY_QUAD_SOURCE: float = 1.00   # 4 sources = same as 3

# ── Market Filters ─────────────────────────────────────────────────────────────
MIN_MARKET_VOLUME: int = 5000
MIN_TIME_TO_CLOSE_HR: int = 2
MAX_TIME_TO_CLOSE_DAYS: int = 30
MAX_BID_ASK_SPREAD: float = 0.05

# ── Time-Decay Edge Discounting ────────────────────────────────────────────────
TIME_DECAY_THRESHOLD_HR: float = 24.0
TIME_DECAY_MIN_MULTIPLIER: float = 0.60

# ── Correlation-Aware Position Sizing ─────────────────────────────────────────
MAX_POSITIONS_PER_CATEGORY: int = 2
CORRELATED_BET_SIZE_PENALTY: float = 0.50

# ── Market Matching ────────────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD: float = 0.75
FUZZY_LOW_CONF_MIN: float = 0.65
DATE_MATCH_TOLERANCE_DAYS: int = 7
MATCH_CACHE_TTL_HOURS: int = 6

# ── API URLs ───────────────────────────────────────────────────────────────────
KALSHI_BASE_URL_PROD: str = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BASE_URL_DEMO: str = "https://demo-api.kalshi.co/trade-api/v2"
MANIFOLD_BASE_URL: str = "https://api.manifold.markets/v0"
PREDICTIT_URL: str = "https://www.predictit.org/api/marketdata/all/"
POLYMARKET_GAMMA_URL: str = "https://gamma-api.polymarket.com"
METACULUS_URL: str = "https://www.metaculus.com/api2/questions/"

# ── Global Source Weights (Vanderbilt 2026 + Calibration City) ────────────────
PREDICTIT_WEIGHT: float = 0.45
MANIFOLD_WEIGHT: float = 0.35
POLYMARKET_WEIGHT: float = 0.20
METACULUS_WEIGHT: float = 0.30   # When matched — renormalised with others

# ── Category-Specific Source Weights ──────────────────────────────────────────
# Override global weights when we know a source excels in a specific domain.
# Values are renormalised to sum to 1.0 within each present source set.
# Source: Calibration City (calibration-by-category analysis), Vanderbilt 2026
CATEGORY_SOURCE_WEIGHTS: dict = {
    "election": {
        # PredictIt 93% on 2024 elections — dominant signal
        "predictit": 0.60, "manifold": 0.25, "polymarket": 0.10, "metaculus": 0.05,
    },
    "trump": {
        "predictit": 0.60, "manifold": 0.25, "polymarket": 0.10, "metaculus": 0.05,
    },
    "biden": {
        "predictit": 0.60, "manifold": 0.25, "polymarket": 0.10, "metaculus": 0.05,
    },
    "fed_rates": {
        # Economics: PredictIt + Manifold strong; Polymarket low volume on macro
        "predictit": 0.50, "manifold": 0.35, "polymarket": 0.10, "metaculus": 0.05,
    },
    "inflation": {
        "predictit": 0.45, "manifold": 0.35, "polymarket": 0.10, "metaculus": 0.10,
    },
    "unemployment": {
        "predictit": 0.45, "manifold": 0.35, "polymarket": 0.10, "metaculus": 0.10,
    },
    "gdp": {
        "predictit": 0.35, "manifold": 0.35, "polymarket": 0.15, "metaculus": 0.15,
    },
    "nba": {
        # Sports: Polymarket highest liquidity and volume; PredictIt low coverage
        "predictit": 0.15, "manifold": 0.30, "polymarket": 0.55, "metaculus": 0.00,
    },
    "nfl": {
        "predictit": 0.15, "manifold": 0.30, "polymarket": 0.55, "metaculus": 0.00,
    },
    "mlb": {
        "predictit": 0.15, "manifold": 0.30, "polymarket": 0.55, "metaculus": 0.00,
    },
    "btc": {
        # Crypto: Polymarket dominates; Manifold decent; PredictIt negligible
        "predictit": 0.05, "manifold": 0.30, "polymarket": 0.65, "metaculus": 0.00,
    },
    "eth": {
        "predictit": 0.05, "manifold": 0.30, "polymarket": 0.65, "metaculus": 0.00,
    },
    "crypto": {
        "predictit": 0.05, "manifold": 0.30, "polymarket": 0.65, "metaculus": 0.00,
    },
    "sp500": {
        # Financial markets: Manifold + PredictIt both decent
        "predictit": 0.40, "manifold": 0.35, "polymarket": 0.20, "metaculus": 0.05,
    },
    # Tech/AI/Science: Metaculus excels, Manifold strong, others weaker
    # These categories aren't in the correlation list but can appear
}

# ── Retry Settings ─────────────────────────────────────────────────────────────
MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: float = 1.0
RATE_LIMIT_WAIT_S: int = 60
SERVER_ERROR_WAIT_S: int = 10
CONCURRENT_MARKET_SCANS: int = 10

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
BRIER_LOG: str = "logs/brier_scores.jsonl"     # NEW: calibration tracking

# ── Dashboard ──────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH_S: int = 30
DAILY_SUMMARY_UTC_HOUR: int = 23
DAILY_SUMMARY_UTC_MINUTE: int = 59
