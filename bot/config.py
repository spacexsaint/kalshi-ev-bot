"""
config.py — All tuneable parameters in one place.

API FINDINGS (verified 2026-04-08):
  Kalshi Base URL (prod):  https://api.elections.kalshi.com/trade-api/v2
  Kalshi Base URL (demo):  https://demo-api.kalshi.co/trade-api/v2
  Auth: RSA-PSS with SHA256. Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE
  Signing message: f"{timestamp_ms}{HTTP_METHOD}{path_without_query}"
  Rate limits: Basic=20r/s read, 10r/s write | Advanced=30/30 | Premier=100/100
  Taker fee: round_up(0.07 × C × P × (1−P))
  Maker fee: round_up(0.0175 × C × P × (1−P))
  INX/NASDAQ100 taker fee: round_up(0.035 × C × P × (1−P))
  Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf

  Manifold Base URL: https://api.manifold.markets/v0
  Endpoint: GET /markets?limit=1000  — probability field: market["probability"] (0–1)
  No auth for reads. Rate limit: 500 req/min per IP.
  Source: https://docs.manifold.markets/api

  PredictIt: GET https://www.predictit.org/api/marketdata/all/
  Implied probability: contract["bestBuyYesCost"] (primary for fair value)
  Also: contract["lastTradePrice"] (last traded price)
  Structure: { "markets": [ { "id", "name", "contracts": [...] } ] }
  No auth required.
  Source: live fetch 2026-04-08
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading Parameters ─────────────────────────────────────────────────────────
MIN_EDGE: float = 0.05          # 5% net-of-fees minimum edge to bet
KELLY_FRACTION: float = 0.25   # Quarter-Kelly for safety
MAX_BET_PCT: float = 0.05      # Max 5% of balance per single bet
MIN_BET_USD: float = 1.00      # Kalshi minimum contract value ($1)
MAX_OPEN_POSITIONS: int = 10   # Simultaneous open positions cap
DAILY_LOSS_LIMIT_PCT: float = 0.15  # Halt trading if down 15% on the day
SCAN_INTERVAL_SEC: int = 300   # 5 minutes between full market scans

# ── Market Filters ─────────────────────────────────────────────────────────────
MIN_MARKET_VOLUME: int = 5000       # Minimum $5,000 total volume
MIN_TIME_TO_CLOSE_HR: int = 2       # Ignore markets closing in < 2 hours
MAX_TIME_TO_CLOSE_DAYS: int = 30    # Ignore markets > 30 days out
MAX_BID_ASK_SPREAD: float = 0.05   # Max 5-cent bid-ask spread (filter illiquid)

# ── Order Management ───────────────────────────────────────────────────────────
ORDER_FILL_TIMEOUT_S: int = 30     # Cancel unfilled orders after 30s
PROFIT_TAKE_CENTS: int = 15        # Close position early if +15 cents in our favor
PRICE_STALENESS_CENTS: int = 2     # Skip if price moved > 2 cents since evaluation

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

# ── Retry Settings ─────────────────────────────────────────────────────────────
MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: float = 1.0    # 1s, 2s, 4s exponential
RATE_LIMIT_WAIT_S: int = 60        # Wait 60s on 429
SERVER_ERROR_WAIT_S: int = 10      # Wait 10s on 5xx
CONCURRENT_MARKET_SCANS: int = 10  # Max concurrent orderbook fetches

# ── Manifold Aggregation Weights ───────────────────────────────────────────────
MANIFOLD_WEIGHT: float = 0.6       # Manifold has better calibration
PREDICTIT_WEIGHT: float = 0.4

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
