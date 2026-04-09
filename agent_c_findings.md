# Agent C — Data Pipeline Audit

**Commit audited:** cd2502f
**Date:** 2026-04-09
**Scope:** fair_value.py, market_matcher.py, kalshi_client.py (market fetch), tests/test_market_matcher.py

## Items Checked: 24
## Bugs Found: 1

---

### External Source Fetching

| # | Check | Result | Location |
|---|-------|--------|----------|
| 1 | PredictIt field: `bestBuyYesCost` with fallback to `lastTradePrice` | ✓ Correct | fair_value.py:179 |
| 2 | Manifold field: `probability` (float 0-1) | ✓ Correct | fair_value.py:220 |
| 3 | Polymarket field: `outcomePrices[0]` (YES price), fallback to `lastTradePrice` | ✓ Correct | fair_value.py:271-281 |
| 4 | Polymarket handles `outcomePrices` as both JSON string and list | ✓ Correct | fair_value.py:272-273 |
| 5 | Network failure: each source returns `[]` independently, bot continues with remaining | ✓ Correct | fair_value.py:170-172, 217, 261 |
| 6 | Retry logic: `_get_json` retries 2x with exponential backoff per source, independently | ✓ Correct | fair_value.py:117-165 |
| 7 | All three sources gather concurrently via `asyncio.gather` | ✓ Correct | fair_value.py:314-317 |

### Probability Aggregation

| # | Check | Result | Location |
|---|-------|--------|----------|
| 8 | Source probs clipped to strict (0,1) BEFORE aggregation — all 3 sources | ✓ Correct | fair_value.py:186-187, 230-231, 283 |
| 9 | Weight renormalization: `total_w = sum(w for present sources)`, divides each by `total_w` | ✓ Correct | fair_value.py:387-398 |
| 10 | If 2 of 3 sources present, weights correctly become `w_i / (w_i + w_j)` | ✓ Correct | fair_value.py:387-398 |
| 11 | Disagreement mult boundary: `>0.10` (strict), so exactly 0.10 gets mult=1.0 | ✓ Correct (consistent) | fair_value.py:414-415 |
| 12 | Single source: `len < 2` returns mult=1.0, numpy `std` never called on single element | ✓ Correct | fair_value.py:410-411 |
| 13 | KL penalty: multiplicative to Kelly stake (in edge_calculator.py:216), NOT additive to MIN_EDGE | ✓ Correct | edge_calculator.py:216, 82-88 |
| 14 | KL penalty and disagreement_mult are separate multipliers, no double-counting | ✓ Correct | edge_calculator.py:216 |
| 15 | All sources None: `_aggregate_probabilities` returns count=0; `get_fair_value` returns None | ✓ Correct | fair_value.py:381-382, 455-456 |

### Market Matching

| # | Check | Result | Location |
|---|-------|--------|----------|
| 16 | Fuzzy threshold 0.75 in config.py (not hardcoded) | ✓ Correct | config.py:103 |
| 17 | Uses `token_sort_ratio` (70%) + `partial_ratio` (30%) — correct for reordered words | ✓ Correct | market_matcher.py:161-165 |
| 18 | Index guard: NASDAQ vs S&P rejection fires correctly | ✓ Correct | market_matcher.py:229-232 |
| 19 | Cache key: `ticker::source` (ticker-based, not title-based — no collision risk) | ✓ Correct | market_matcher.py:136 |
| 20 | Score floor: below `FUZZY_LOW_CONF_MIN` → None, silently; in [0.65, 0.75) → None with log | ✓ Correct | market_matcher.py:211-224 |

### Cache System

| # | Check | Result | Location |
|---|-------|--------|----------|
| 21 | fair_value uses `asyncio.Lock` (lazy-init), correct for async code | ✓ Correct | fair_value.py:70, 79-83 |
| 22 | Cache TTL uses `time.monotonic()` (immune to clock jumps), not `datetime` | ✓ Correct | fair_value.py:322, 332 |
| 23 | Double-check pattern: `_caches_valid()` re-checked inside lock | ✓ Correct | fair_value.py:312 |
| 24 | **Match cache unbounded growth**: `_cache` dict grows forever, no max size or eviction | ✗ **BUG — FIXED** | market_matcher.py:247 |

---

### ✗ Bug Fixed: Match Cache Unbounded Growth (memory leak)

**Problem:** The `_cache` dict in `market_matcher.py` grows without limit. Every unique `{ticker}::{source}` pair is stored forever. With 3 sources x thousands of Kalshi tickers, this causes:
- Unbounded memory consumption on long-running bot sessions
- Progressively more expensive `_save_cache_to_disk()` calls (serializes entire dict on every match)

**Fix:**
- Added `MATCH_CACHE_MAX_ENTRIES = 5000` to `config.py:107`
- Added FIFO eviction in `market_matcher.py:250-253`: when cache exceeds max, oldest entries (by insertion order, Python 3.7+ dict guarantee) are evicted

**Files changed:**
- `bot/config.py` — added `MATCH_CACHE_MAX_ENTRIES`
- `bot/market_matcher.py` — added eviction logic after cache insert

### Tests Added (9 new tests)

**tests/test_market_matcher.py:**
1. `TestCacheEviction::test_config_has_max_entries` — config constant exists
2. `TestCacheEviction::test_cache_evicts_oldest_when_full` — eviction triggers at max
3. `TestCacheEviction::test_cache_key_is_ticker_based` — key format verification
4. `TestAggregationEdgeCases::test_all_sources_none_returns_zero_count` — zero sources
5. `TestAggregationEdgeCases::test_weight_renormalization_two_sources` — renorm math
6. `TestAggregationEdgeCases::test_disagreement_boundary_exactly_010` — boundary at 0.10

**tests/test_fair_value.py:**
7. `TestFairValueCacheSystem::test_cache_uses_monotonic_time` — monotonic, not datetime
8. `TestFairValueCacheSystem::test_refresh_lock_is_asyncio_not_threading` — async lock type
9. `TestFairValueCacheSystem::test_aggregate_zero_sources_returns_none_via_get_fair_value` — None, not 0.5

**Test results:** 57 passed, 0 failed (48 original + 9 new)
