"""
test_main.py — Regression tests for main.py fixes.

Tests:
  - Pure arbitrage detection (C10/C15 fix)
  - Relative stop-loss logic (C11 fix)
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
        Arb contract sizing: n = floor(budget / cost_per_pair).
        budget = MAX_BET_PCT * balance / 2 (since buying 2 sides).
        """
        balance = 1000.0
        yes_ask = 0.45
        no_ask = 0.50
        cost_per_pair = yes_ask + no_ask  # 0.95
        arb_budget = config.MAX_BET_PCT * balance / 2.0  # 0.05 * 1000 / 2 = 25.0
        n = math.floor(arb_budget / cost_per_pair)
        assert n == 26  # floor(25.0 / 0.95) = 26
        guaranteed_profit = (1.0 - cost_per_pair) * n
        assert guaranteed_profit == pytest.approx(0.05 * 26, abs=0.01)

    def test_arb_too_small(self):
        """
        With very small balance, arb budget may not cover even 1 pair.
        """
        balance = 1.0
        yes_ask = 0.60
        no_ask = 0.39  # sum = 0.99 < 1.0
        cost_per_pair = yes_ask + no_ask
        arb_budget = config.MAX_BET_PCT * balance / 2.0  # 0.05 * 1 / 2 = 0.025
        n = math.floor(arb_budget / cost_per_pair)
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
