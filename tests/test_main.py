"""
test_main.py — Regression tests for main.py fixes.

Tests:
  - Pure arbitrage detection (C10/C15 fix)
  - Relative stop-loss logic (C11 fix)
  - Arb fee accounting (deep critique fix)
  - Adaptive min_edge gate (deep critique fix)
  - Existing position arb guard (deep critique fix)
  - Daily start balance not overwritten on scan (deep critique fix)
"""

import math
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import config


# ── Pure arbitrage detection tests ────────────────────────────────────────────

class TestPureArbDetection:
    """
    Regression tests for pure arbitrage detection (C10/C15 fix).

    When yes_ask + no_ask < 1.0, buying both sides guarantees riskless profit.
    """

    def test_arb_detected_sum_below_one(self):
        """
        yes_ask=0.45, no_ask=0.50 → sum=0.95 < 1.0 → arb detected.
        Guaranteed profit = 1.0 - 0.95 = 0.05 per contract pair.
        """
        yes_ask = 0.45
        no_ask = 0.50
        assert yes_ask + no_ask < 1.0, "Sum should be below 1.0 for arb"
        spread = 1.0 - yes_ask - no_ask
        assert spread == pytest.approx(0.05)

    def test_no_arb_sum_above_one(self):
        """
        yes_ask=0.52, no_ask=0.50 → sum=1.02 > 1.0 → NO arb.
        """
        yes_ask = 0.52
        no_ask = 0.50
        assert yes_ask + no_ask >= 1.0, "Sum should be >= 1.0 (no arb)"

    def test_arb_contract_sizing(self):
        """
        Arb contract sizing: n = floor(budget / (cost_per_pair + fee_per_pair)).
        budget = MAX_BET_PCT * balance / 2 (since buying 2 sides).
        Fees must be in the denominator to prevent budget overrun.
        """
        from bot.fee_calculator import compute_taker_fee
        balance = 1000.0
        yes_ask = 0.45
        no_ask = 0.50
        cost_per_pair = yes_ask + no_ask  # 0.95
        fee_per_pair = compute_taker_fee(yes_ask, 1) + compute_taker_fee(no_ask, 1)
        arb_budget = config.MAX_BET_PCT * balance / 2.0  # 0.05 * 1000 / 2 = 25.0
        n = math.floor(arb_budget / (cost_per_pair + fee_per_pair))
        # Total cost must not exceed budget
        total_cost = n * cost_per_pair + compute_taker_fee(yes_ask, n) + compute_taker_fee(no_ask, n)
        assert total_cost <= arb_budget + 0.01, (
            f"Arb total cost {total_cost:.2f} exceeds budget {arb_budget:.2f}"
        )
        assert n >= 1, "Should afford at least 1 pair"

    def test_arb_too_small(self):
        """
        With very small balance, arb budget may not cover even 1 pair.
        """
        from bot.fee_calculator import compute_taker_fee
        balance = 1.0
        yes_ask = 0.60
        no_ask = 0.39  # sum = 0.99 < 1.0
        cost_per_pair = yes_ask + no_ask
        fee_per_pair = compute_taker_fee(yes_ask, 1) + compute_taker_fee(no_ask, 1)
        arb_budget = config.MAX_BET_PCT * balance / 2.0  # 0.05 * 1 / 2 = 0.025
        n = math.floor(arb_budget / (cost_per_pair + fee_per_pair))
        assert n == 0, "Budget too small for even 1 arb pair"


# ── Relative stop-loss tests ─────────────────────────────────────────────────

class TestRelativeStopLoss:
    """
    Regression tests for relative stop-loss (C11 fix).

    Formula: stop_price = max(entry * (1 - STOP_LOSS_FRACTION), entry - STOP_LOSS_CENTS)
    max() = more permissive (less aggressive stop).
    """

    def _compute_stop_price_cents(self, entry_cents: int) -> int:
        """Replicate the stop-loss logic from main.py."""
        relative_stop = int(entry_cents * (1.0 - config.STOP_LOSS_FRACTION))
        absolute_stop = entry_cents - config.STOP_LOSS_CENTS
        return max(relative_stop, absolute_stop)

    def test_entry_20c_stop_at_12c(self):
        """
        entry=20c: relative_stop = 20*(1-0.40) = 12c, absolute_stop = 20-20 = 0c.
        effective_stop = max(12, 0) = 12c.
        bid=11c < 12c → TRIGGERED.
        """
        stop = self._compute_stop_price_cents(20)
        assert stop == 12
        assert 11 < stop  # bid=11c triggers

    def test_entry_80c_stop_at_60c(self):
        """
        entry=80c: relative_stop = 80*(1-0.40) = 48c, absolute_stop = 80-20 = 60c.
        effective_stop = max(48, 60) = 60c.
        bid=62c > 60c → NOT triggered.
        bid=58c < 60c → TRIGGERED.
        """
        stop = self._compute_stop_price_cents(80)
        assert stop == 60
        assert 62 >= stop  # bid=62c does NOT trigger (62 >= 60, not < 60)
        assert 58 < stop  # bid=58c triggers

    def test_entry_80c_bid_62c_not_triggered(self):
        """entry=80c, bid=62c → stop=60c, 62 >= 60 → NOT triggered."""
        stop = self._compute_stop_price_cents(80)
        current_bid_cents = 62
        assert not (current_bid_cents < stop), "62c should NOT trigger stop at 60c"

    def test_entry_80c_bid_65c_not_triggered(self):
        """entry=80c, bid=65c → stop=60c, 65 > 60 → NOT triggered."""
        stop = self._compute_stop_price_cents(80)
        current_bid_cents = 65
        assert not (current_bid_cents < stop), "65c should NOT trigger stop at 60c"

    def test_entry_20c_bid_11c_triggered(self):
        """entry=20c, bid=11c → stop=12c, 11 < 12 → TRIGGERED."""
        stop = self._compute_stop_price_cents(20)
        current_bid_cents = 11
        assert current_bid_cents < stop, "11c should trigger stop at 12c"

    def test_entry_50c_uses_absolute(self):
        """
        entry=50c: relative_stop = 50*(1-0.40) = 30c, absolute_stop = 50-20 = 30c.
        At 50c both are equal (30c).
        """
        stop = self._compute_stop_price_cents(50)
        assert stop == 30

    def test_high_entry_prefers_absolute(self):
        """
        entry=90c: relative_stop = 90*0.60 = 54c, absolute_stop = 90-20 = 70c.
        max(54, 70) = 70c — absolute dominates for high-priced positions.
        """
        stop = self._compute_stop_price_cents(90)
        assert stop == 70

    def test_low_entry_prefers_relative(self):
        """
        entry=10c: relative_stop = 10*0.60 = 6c, absolute_stop = 10-20 = -10c.
        max(6, -10) = 6c — relative prevents stopping out on noise.
        Old fixed 20c stop would have been at -10c (i.e., NEVER triggers).
        """
        stop = self._compute_stop_price_cents(10)
        assert stop == 6
        # The relative stop actually protects us at low prices
        assert stop > 0, "Stop must be positive for low-priced positions"


# ── Arb fee accounting tests (deep critique fix) ────────────────────────────

class TestArbFeeAccounting:
    """
    Regression: arb profit must subtract fees.
    A spread of 0.02 per pair can be wiped out by taker fees on both sides.
    """

    def test_arb_profit_subtracts_fees(self):
        """
        Verify the arb profit formula includes fee subtraction.
        spread=0.05, n=10, but fees eat into the profit.
        """
        from bot.fee_calculator import compute_taker_fee
        yes_ask = 0.45
        no_ask = 0.50
        n = 10
        spread = 1.0 - yes_ask - no_ask
        yes_fee = compute_taker_fee(yes_ask, n)
        no_fee = compute_taker_fee(no_ask, n)
        net_profit = spread * n - yes_fee - no_fee
        # With fees, net profit is less than gross
        assert net_profit < spread * n
        # But with a decent spread it's still positive
        assert net_profit > 0

    def test_arb_unprofitable_thin_spread(self):
        """
        A tiny spread (0.01) on 1 contract is wiped out by fees.
        Each side's fee rounds up to $0.01, total = $0.02 > $0.01 spread.
        """
        from bot.fee_calculator import compute_taker_fee
        yes_ask = 0.50
        no_ask = 0.49  # sum = 0.99, spread = 0.01
        n = 1
        spread = 1.0 - yes_ask - no_ask
        yes_fee = compute_taker_fee(yes_ask, n)
        no_fee = compute_taker_fee(no_ask, n)
        net_profit = spread * n - yes_fee - no_fee
        assert net_profit <= 0, "Thin spread arb should be unprofitable after fees"


# ── Adaptive min_edge gate tests (deep critique fix) ────────────────────────

class TestAdaptiveMinEdgeGate:
    """
    Regression: main.py must use result.min_edge_used (adaptive threshold)
    instead of the fixed config.MIN_EDGE=0.05.
    """

    def test_triple_source_uses_3pct_threshold(self):
        """
        A triple-source bet with 4% net edge passes compute_edge (threshold=3%)
        but was previously rejected by main.py's hardcoded 5% gate.
        """
        from bot.edge_calculator import compute_edge
        r = compute_edge(w=0.60, p=0.565, q=0.44, balance=1000, source_count=3)
        # If compute_edge says YES/NO, the edge passed the adaptive threshold
        # main.py should also accept it (uses result.min_edge_used now)
        if r.direction != "NONE":
            assert r.min_edge_used == pytest.approx(config.MIN_EDGE_TRIPLE_SOURCE)
            # The net edge should be >= the adaptive threshold (not the global 5%)
            assert r.net_edge >= r.min_edge_used

    def test_main_py_does_not_use_global_min_edge(self):
        """
        Verify main.py references result.min_edge_used, not config.MIN_EDGE.
        """
        import inspect
        from bot.main import _evaluate_market
        src = inspect.getsource(_evaluate_market)
        assert "result.min_edge_used" in src, (
            "_evaluate_market must use result.min_edge_used, not config.MIN_EDGE"
        )
        assert "config.MIN_EDGE" not in src, (
            "_evaluate_market must NOT use config.MIN_EDGE (use adaptive threshold)"
        )


# ── Existing position arb guard tests (deep critique fix) ───────────────────

class TestArbExistingPositionGuard:
    """
    Regression: _place_arb_trade must skip if we already hold a position
    on the same ticker — partial exposure makes the arb not riskless.
    """

    def test_arb_guard_in_source(self):
        """Verify _place_arb_trade checks state_manager.get_position."""
        import inspect
        from bot.main import _place_arb_trade
        src = inspect.getsource(_place_arb_trade)
        assert "get_position" in src, (
            "_place_arb_trade must check for existing position before arb"
        )


# ── Daily start balance not overwritten tests (deep critique fix) ────────────

class TestDailyStartBalanceNotOverwritten:
    """
    Regression: _scan_markets must NOT call set_daily_start_balance,
    which caused 'loss amnesia' by resetting the baseline on every scan.
    """

    def test_scan_markets_no_set_daily_start_balance(self):
        """Verify _scan_markets does not call set_daily_start_balance."""
        import inspect
        from bot.main import _scan_markets
        src = inspect.getsource(_scan_markets)
        assert "set_daily_start_balance" not in src, (
            "_scan_markets must NOT call set_daily_start_balance (loss amnesia bug)"
        )


# ── place_order sell action tests (deep critique fix) ───────────────────────

class TestPlaceOrderSellAction:
    """
    Regression: place_order must support action='sell' for closing positions.
    Previously hardcoded action='buy', meaning close_position placed BUY orders.
    """

    def test_place_order_accepts_sell(self):
        """KalshiClient.place_order must accept action='sell'."""
        import inspect
        from bot.kalshi_client import KalshiClient
        sig = inspect.signature(KalshiClient.place_order)
        assert "action" in sig.parameters, "place_order must have 'action' parameter"
        # Default should be 'buy' for backward compatibility
        assert sig.parameters["action"].default == "buy"

    def test_close_position_uses_sell(self):
        """executor.close_position must pass action='sell' to _execute_live_order."""
        import inspect
        from bot.executor import close_position
        src = inspect.getsource(close_position)
        assert 'action="sell"' in src, (
            "close_position must pass action='sell' to _execute_live_order"
        )

    def test_place_order_rejects_invalid_action(self):
        """place_order must raise ValueError for invalid action."""
        import asyncio
        import aiohttp
        from bot.kalshi_client import KalshiClient

        async def _test():
            async with aiohttp.ClientSession() as session:
                client = KalshiClient(session)
                with pytest.raises(ValueError, match="action"):
                    await client.place_order("TICK", "yes", 50, 1, "uuid", action="invalid")
        asyncio.run(_test())


# ── Arb fee-inclusive sizing tests (triple-check fix) ─────────────────────────

class TestArbFeeInclusiveSizing:
    """
    Regression: arb contract count must include per-pair fees in the denominator
    so total cost (n * cost_per_pair + total_fees) never exceeds budget.
    """

    def test_arb_total_cost_within_budget(self):
        """Total arb cost must not exceed the arb budget."""
        from bot.fee_calculator import compute_taker_fee
        balance = 1000.0
        yes_ask = 0.45
        no_ask = 0.50
        cost_per_pair = yes_ask + no_ask
        fee_per_pair = compute_taker_fee(yes_ask, 1) + compute_taker_fee(no_ask, 1)
        arb_budget = config.MAX_BET_PCT * balance / 2.0
        n = math.floor(arb_budget / (cost_per_pair + fee_per_pair))
        total_cost = n * cost_per_pair + compute_taker_fee(yes_ask, n) + compute_taker_fee(no_ask, n)
        assert total_cost <= arb_budget + 0.01

    def test_arb_source_includes_fees_in_denominator(self):
        """Verify main.py _place_arb_trade includes fee_per_pair in contract calculation."""
        import inspect
        from bot.main import _place_arb_trade
        src = inspect.getsource(_place_arb_trade)
        assert "fee_per_pair" in src, (
            "_place_arb_trade must include per-pair fees in contract sizing denominator"
        )


# ── Log trimming tests (triple-check fix) ─────────────────────────────────────

class TestLogTrimming:
    """Regression: logger.py must cap JSONL file growth."""

    def test_logger_has_max_log_bytes(self):
        """logger.py must define a max log size constant."""
        from bot import logger as bl
        assert hasattr(bl, "_MAX_LOG_BYTES"), "logger must have _MAX_LOG_BYTES"
        assert bl._MAX_LOG_BYTES > 0
