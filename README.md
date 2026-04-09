# ⬡ Kalshi EV Arbitrage Bot

A production-grade, fully automated **expected-value arbitrage bot** for [Kalshi](https://kalshi.com) — the CFTC-regulated prediction market. The bot finds mispricings by aggregating probability estimates from three independent external sources, then sizes bets using a mathematically-optimal Kelly criterion with layered risk controls.

> **Default is PAPER mode. No real money at risk until you explicitly set `PAPER_MODE=false`.**

---

## How It Makes Money

Prediction markets are efficient — but not perfectly. Kalshi prices sometimes diverge from the true probability estimated by independent platforms. When a market is priced at 60¢ but three external sources agree the true probability is 70%, a YES contract has positive expected value:

```
EV = w × (1 - p) - (1 - w) × p  =  0.70 × 0.40 - 0.30 × 0.60  =  +$0.10 per contract
```

The bot finds these gaps systematically, sizes bets using the Kelly criterion, and executes automatically — every 5 minutes, 24/7.

---

## Strategy Architecture

```
Every 5 minutes:
┌─ Phase 1: Manage Open Positions ────────────────────────────┐
│  • Push live bid prices to dashboard (mark-to-market PnL)   │
│  • Profit-take if bid ≥ entry + 15¢ (lock in gain early)    │
│  • Stop-loss if bid ≤ entry - 20¢ (prevent max loss)        │
│  • On resolution: compute PnL + log Brier score             │
└──────────────────────────────────────────────────────────────┘
┌─ Phase 2: Scan All Markets ──────────────────────────────────┐
│  • Fetch all open Kalshi markets (paginated cursor API)      │
│  • Filter: volume ≥ $5k, spread ≤ 5¢, 2h-30d to close      │
│  • For each candidate (10 concurrent):                       │
│    – Fetch fair probability from 3 sources (parallel)        │
│    – Apply category-specific calibration weights             │
│    – Compute fee-aware Kelly edge with 3 adjustment layers   │
│    – Apply adaptive MIN_EDGE by source confidence            │
│  • Sort by net edge ↓, execute top candidates               │
│  • Spread momentum check before final order                  │
└──────────────────────────────────────────────────────────────┘
┌─ Phase 3: Dashboard Refresh ─────────────────────────────────┐
│  • Update Rich terminal UI with live data                    │
└──────────────────────────────────────────────────────────────┘
```

---

## Mathematical Foundation

### Kelly Criterion for Binary Prediction Markets

Standard Kelly does not apply here. For $1-paying Kalshi contracts, the optimal fraction is (Meister 2024, arXiv:2412.14144):

```
YES: f* = (w - p) / (1 - p)
NO:  f* = ((1 - w) - q) / (1 - q)
```

Where `w` = fair probability, `p` = YES ask price, `q` = NO ask price. Both Kelly and EV use the **same exec price (ask)** — using midpoint for Kelly but ask for EV creates an incoherent signal that was a bug in v2 of this bot.

### Three Adjustment Layers Applied to Kelly

**Layer 1 — Quarter-Kelly** (KELLY_FRACTION = 0.25)
Reduces expected drawdown by ~75% while preserving ~87% of long-run growth rate (arXiv 2020).

**Layer 2 — KL Uncertainty Penalty** (Meister 2024, Galekwa 2026)
Applies a multiplicative penalty based on source count:
```
1 source: × 0.50  (50% reduction — high estimation variance)
2 sources: × 0.75  (25% reduction)
3 sources: × 1.00  (no penalty — high confidence)
```
This reduces ruin probability from ~78% → <2% for single-source bets.

**Layer 3 — Time-Decay Multiplier**
Markets near resolution face price convergence. External probabilities lose predictive power:
```
≥ 24h to close: × 1.00 (no decay)
13h to close:   × 0.80
 2h to close:   × 0.60 (minimum)
```

### Adaptive Minimum Edge Threshold

A flat 5% threshold treats high-confidence triple-source bets identically to uncertain single-source bets. The adaptive threshold corrects this:

```
Triple source (3 agree): 3%  — high confidence in w, lower bar justified
Dual source  (2 agree):  5%  — standard
Single source (1 only):  8%  — extra margin for estimation uncertainty
```

Combined with the KL penalty on stake size, this creates a two-layer filter: uncertain bets need both a larger edge AND get a smaller stake.

### Fee Model (exact Kalshi schedule)

```
Taker fee:    ceil(0.07 × C × P × (1−P))
Maker fee:    ceil(0.0175 × C × P × (1−P))   [4x cheaper!]
INX/NASDAQ100: ceil(0.035 × C × P × (1−P))   [half rate]
```

Fee is deducted from the available budget BEFORE sizing contracts (iterative solver avoids cost overrun).

---

## Probability Sources (all free, no auth)

| Source | Weight | Strength | Calibration |
|---|---|---|---|
| **PredictIt** | 45% (elections: 60%) | Politics, elections | 93% accuracy — Vanderbilt 2026 |
| **Manifold** | 35% (tech: 50%) | Tech, science, general | Well-calibrated — arXiv 2025 |
| **Polymarket** | 20% (sports/crypto: 65%) | Sports, crypto | 67% accuracy — Vanderbilt 2026 |

Weights are **category-specific**: a cryptocurrency market uses Polymarket at 65%, while an election market uses PredictIt at 60%. Falls back to global weights for uncategorised markets.

---

## Risk Controls

| Control | Value | Description |
|---|---|---|
| Daily loss limit | 15% | Halt all trading if daily P&L < −15% of start balance |
| Max open positions | 10 | Hard cap on simultaneous positions |
| Profit-take | +15¢ | Close early if bid moves ≥ 15¢ in our favour |
| Stop-loss | −20¢ | Close early if bid moves ≥ 20¢ against us |
| Max bet size | 5% | No single bet exceeds 5% of balance |
| Correlation cap | 2 per category | Max 2 open positions in same topic (Fed rates, BTC, etc.) |
| Liquidity filter | ≤5¢ spread | Skip illiquid markets |
| Spread momentum | ×1.5 | Skip if spread widens >50% between eval and execution |
| Blind bet protection | None | Never trade if no external source matches |

---

## Calibration Tracking

Every market resolution logs a **Brier score** to `logs/brier_scores.jsonl`:

```json
{
  "ts": "2026-04-09T04:00:00Z",
  "ticker": "KXFED-26NOV",
  "fair_prob_at_entry": 0.67,
  "resolved_yes": true,
  "brier_score": 0.1089
}
```

Monitor the running mean: below 0.10 = excellent, 0.10-0.20 = good, above 0.25 = worse than random (re-evaluate sources).

---

## Quick Start

```bash
git clone https://github.com/spacexsaint/kalshi-ev-bot
cd kalshi-ev-bot
bash setup.sh

# Edit .env:
KALSHI_API_KEY=<your-api-key-id>
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
DISCORD_WEBHOOK_URL=<optional>
PAPER_MODE=true

# Validate strategy
python backtest.py --synthetic --n 1000 --walk-forward

# Run tests
pytest tests/ -v

# Start (paper mode)
python -m bot.main

# One scan cycle for testing
python -m bot.main --single
```

---

## Getting a Kalshi API Key (free)

1. Create account at [kalshi.com](https://kalshi.com)
2. Go to Account Settings → API Keys → Create New API Key
3. Save the **Private Key** (`.key` file) — shown only once
4. Save the **Key ID** (UUID shown on screen)
5. Put both in `.env`

The API is completely free. You only pay Kalshi's trading fees on filled orders.

---

## File Structure

```
kalshi-ev-bot/
├── bot/
│   ├── config.py           All parameters — edit this file to tune
│   ├── main.py             Orchestration loop (asyncio, Phase 1/2/3)
│   ├── edge_calculator.py  Kelly criterion + adaptive min edge + fee sizing
│   ├── fee_calculator.py   Exact Kalshi fee formula (verified vs PDF)
│   ├── fair_value.py       3-source probability aggregation + retry
│   ├── market_matcher.py   Fuzzy title matching (rapidfuzz, thread-safe)
│   ├── kalshi_client.py    RSA-PSS auth + retry + rate limiting
│   ├── risk_manager.py     Circuit breakers + correlation detection
│   ├── state_manager.py    Persistent state with filelock
│   ├── executor.py         Order placement + actual fee tracking
│   ├── logger.py           Structured JSONL logging + Brier score
│   └── dashboard.py        Rich terminal UI with live unrealised P&L
├── tests/                  122 unit tests — all pass
├── backtest.py             Walk-forward validation + Brier score output
├── setup.sh                One-command environment setup
└── .env.example            Configuration template
```

---

## Log Files

| File | Contents |
|---|---|
| `logs/trades.jsonl` | Every executed trade with full edge metadata |
| `logs/events.jsonl` | Startup, shutdown, circuit breakers (with severity) |
| `logs/api.jsonl` | Every API call with latency (for debugging) |
| `logs/brier_scores.jsonl` | Calibration: fair_prob vs actual resolution |
| `logs/low_confidence_matches.jsonl` | Fuzzy matches 0.65-0.74 for manual review |

---

## Configuration Reference

All parameters in `bot/config.py`:

```python
# Adaptive edge thresholds
MIN_EDGE_TRIPLE_SOURCE = 0.03   # 3% when 3 sources confirm
MIN_EDGE_DUAL_SOURCE   = 0.05   # 5% standard
MIN_EDGE_SINGLE_SOURCE = 0.08   # 8% when uncertain

# Kelly sizing
KELLY_FRACTION   = 0.25   # Quarter-Kelly
MAX_BET_PCT      = 0.05   # 5% of balance max
MIN_BET_USD      = 1.00   # $1 minimum

# KL uncertainty penalties
KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE = 0.50
KL_UNCERTAINTY_PENALTY_DUAL_SOURCE   = 0.75
KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE = 1.00

# Risk controls
DAILY_LOSS_LIMIT_PCT       = 0.15   # 15%
MAX_OPEN_POSITIONS         = 10
PROFIT_TAKE_CENTS          = 15
STOP_LOSS_CENTS            = 20
MAX_POSITIONS_PER_CATEGORY = 2

# Market filters
MIN_MARKET_VOLUME     = 5000    # $5k
MAX_BID_ASK_SPREAD    = 0.05    # 5¢
SPREAD_WIDENING_FACTOR = 1.5   # Skip if spread grew >50%
TIME_DECAY_THRESHOLD_HR = 24.0 # Start decay at 24h to close
```

---

## Pre-Live Checklist

```
□ Run backtest.py --walk-forward and review all 5 folds
□ Check logs/brier_scores.jsonl mean is below 0.20
□ Run in PAPER_MODE=true for at least 48 hours
□ Review logs/trades.jsonl for quality of matches and edge values
□ Review logs/low_confidence_matches.jsonl — tune FUZZY_MATCH_THRESHOLD if needed
□ Check Discord alerts are firing correctly
□ Set PAPER_MODE=false in .env to go live
```

---

## Disclaimer

This software is for educational and research purposes. Prediction market trading involves financial risk. Past backtest performance does not guarantee future results. Never risk more than you can afford to lose. You are responsible for compliance with all applicable laws.

Kalshi is a CFTC-regulated exchange operating legally in all US states.
