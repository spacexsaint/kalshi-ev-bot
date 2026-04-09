# ⬡ Kalshi EV Arbitrage Bot

A production-grade, fully automated expected-value (EV) arbitrage trading bot for [Kalshi](https://kalshi.com) — the CFTC-regulated prediction market. The bot finds mispricings by comparing Kalshi market prices to independent probability estimates from Manifold Markets and PredictIt, then places bets when the edge exceeds a minimum threshold, net of all fees.

> **Default mode is PAPER (simulated) — no real money at risk until you explicitly set `PAPER_MODE=false`.**

---

## How It Works

```
Every 5 minutes:
  ┌─ Phase 1: Position Management ──────────────────────────────────┐
  │  • Check each open position's current price                     │
  │  • Profit-take if +15¢ in our favor                             │
  │  • Settle resolved markets and record final PnL                 │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ Phase 2: Market Scanning ───────────────────────────────────────┐
  │  • Fetch all open Kalshi markets (paginated)                     │
  │  • Filter by volume, time-to-close, binary type                  │
  │  • Fetch orderbooks (up to 10 concurrent)                        │
  │  • Fetch fair probability from Manifold + PredictIt              │
  │  • Fuzzy-match titles to find corresponding external markets     │
  │  • Compute Kelly-criterion edge, net of taker fees               │
  │  • Execute top candidates (highest edge first)                   │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ Phase 3: Dashboard Update ──────────────────────────────────────┐
  │  • Refresh Rich terminal UI                                       │
  └──────────────────────────────────────────────────────────────────┘
```

### Edge Formula

```
For YES at price p, fair probability w:
  f_yes = (w - p) / (1 - p)
  gross_ev = w × (1-p) − (1-w) × p
  net_ev   = gross_ev − taker_fee
  net_edge = net_ev / p

  Only bet if net_edge >= 5% (MIN_EDGE)
```

### Fee Model (exact Kalshi formula, source: kalshi.com/docs/kalshi-fee-schedule.pdf)

```
Taker: fee = ceil(0.07 × C × P × (1 − P))
Maker: fee = ceil(0.0175 × C × P × (1 − P))
INX/NASDAQ100: fee = ceil(0.035 × C × P × (1 − P))
```

---

## Project Structure

```
kalshi-ev-bot/
├── bot/
│   ├── config.py           — All tuneable parameters
│   ├── main.py             — Orchestration loop (asyncio, 5-min scans)
│   ├── kalshi_client.py    — Kalshi API v2 (RSA-PSS auth, retry logic)
│   ├── fair_value.py       — Manifold + PredictIt probability aggregation
│   ├── market_matcher.py   — Fuzzy title matching (rapidfuzz)
│   ├── edge_calculator.py  — Kelly criterion + fee-adjusted EV
│   ├── fee_calculator.py   — Exact Kalshi fee formula
│   ├── executor.py         — Order placement, fill tracking, Discord alerts
│   ├── risk_manager.py     — Circuit breakers, position limits
│   ├── state_manager.py    — Persistent state (filelock)
│   ├── logger.py           — Structured JSON logging (.jsonl)
│   └── dashboard.py        — Rich CLI terminal dashboard
├── tests/
│   ├── test_fee_calculator.py
│   ├── test_edge_calculator.py
│   ├── test_market_matcher.py
│   └── test_risk_manager.py
├── backtest.py             — Backtesting engine (synthetic or live data)
├── setup.sh                — One-command environment setup
├── .env.example            — Environment variable template
├── requirements.txt        — Pinned dependencies
└── README.md
```

---

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/<your-username>/kalshi-ev-bot.git
cd kalshi-ev-bot
bash setup.sh

# 2. Add your credentials to .env
nano .env
# Set: KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, DISCORD_WEBHOOK_URL

# 3. Run the test suite
source venv/bin/activate
pytest tests/ -v

# 4. Run the backtest
python backtest.py

# 5. Start the bot in paper mode
python -m bot.main

# Or: single scan cycle (for testing)
python -m bot.main --single

# 6. Go live (after 48+ hours of paper trading)
# Set PAPER_MODE=false in .env
```

---

## Configuration (`bot/config.py`)

| Parameter | Default | Description |
|---|---|---|
| `MIN_EDGE` | 0.05 | 5% net-of-fees minimum edge |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly for safety |
| `MAX_BET_PCT` | 0.05 | Max 5% of balance per bet |
| `MIN_BET_USD` | 1.00 | Minimum contract stake |
| `MAX_OPEN_POSITIONS` | 10 | Simultaneous positions cap |
| `DAILY_LOSS_LIMIT_PCT` | 0.15 | Halt at 15% daily loss |
| `SCAN_INTERVAL_SEC` | 300 | 5 minutes between scans |
| `MIN_MARKET_VOLUME` | 5000 | Minimum $5k volume filter |
| `MIN_TIME_TO_CLOSE_HR` | 2 | Ignore markets closing in < 2h |
| `MAX_TIME_TO_CLOSE_DAYS` | 30 | Ignore markets > 30 days out |
| `MAX_BID_ASK_SPREAD` | 0.05 | Max 5¢ spread (liquidity filter) |
| `ORDER_FILL_TIMEOUT_S` | 30 | Cancel unfilled orders after 30s |
| `PROFIT_TAKE_CENTS` | 15 | Close position if +15¢ in favor |
| `FUZZY_MATCH_THRESHOLD` | 0.75 | Min fuzzy match score (0–1) |
| `PAPER_MODE` | True | Simulation mode — safe default |

---

## Kalshi API Details

| Item | Value |
|---|---|
| Base URL (prod) | `https://api.elections.kalshi.com/trade-api/v2` |
| Base URL (demo) | `https://demo-api.kalshi.co/trade-api/v2` |
| Auth | RSA-PSS SHA-256 via `KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP` headers |
| Rate limit (Basic) | 20 read/s, 10 write/s |
| Markets endpoint | `GET /markets` |
| Orderbook | `GET /markets/{ticker}/orderbook` |
| Place order | `POST /portfolio/orders` |
| Cancel order | `DELETE /portfolio/orders/{order_id}` |
| Balance | `GET /portfolio/balance` |
| Positions | `GET /portfolio/positions` |

---

## Log Files

| File | Contents |
|---|---|
| `logs/trades.jsonl` | Every executed trade |
| `logs/events.jsonl` | Startup, shutdown, circuit breakers |
| `logs/api.jsonl` | Every API call with latency |
| `logs/low_confidence_matches.jsonl` | Fuzzy matches below threshold |

---

## Safety Checklist Before Going Live

- [ ] Add `KALSHI_API_KEY` and private key to `.env`
- [ ] Add `DISCORD_WEBHOOK_URL` to `.env`
- [ ] Run `python backtest.py` and review output
- [ ] Run in `PAPER_MODE=true` for at least 48 hours
- [ ] Review `logs/trades.jsonl` for quality of matches
- [ ] Review `logs/low_confidence_matches.jsonl` to tune fuzzy threshold
- [ ] Set `PAPER_MODE=false` in `.env` to go live

---

## Disclaimer

This software is for educational and research purposes. Prediction market trading involves financial risk. Past performance of the strategy in backtesting or paper mode does not guarantee future results. Never risk more than you can afford to lose.

Kalshi is a CFTC-regulated exchange. You are responsible for compliance with all applicable laws and regulations in your jurisdiction.
