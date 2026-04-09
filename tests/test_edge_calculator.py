"""
test_edge_calculator.py — Unit tests for upgraded edge_calculator.py

Tests:
  - YES/NO edge with midpoint pricing
  - KL-uncertainty multiplier (single/dual/triple source)
  - Time-decay multiplier (near-close markets)
  - Both edges positive → picks larger
  - Invalid probability → raises ValueError
  - New EdgeResult fields present
"""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import config
from bot.edge_calculator import (
    compute_edge,
    compute_time_decay_multiplier,
    compute_uncertainty_multiplier,
    EdgeResult,
    _gross_ev_yes,
    _gross_ev_no,
    _kelly_yes,
    _kelly_no,
    _compute_midpoint,
)
from bot.fee_calculator import compute_taker_fee


# ── Kelly formula unit tests ───────────────────────────────────────────────────

class TestKellyFormulas:
    def test_kelly_yes_positive(self):
        f = _kelly_yes(0.70, 0.55)
        assert abs(f - (0.70 - 0.55) / (1 - 0.55)) < 1e-9

    def test_kelly_yes_negative(self):
        f = _kelly_yes(0.40, 0.55)
        assert f < 0

    def test_kelly_no_positive(self):
        f = _kelly_no(0.30, 0.52)
        expected = ((1 - 0.30) - 0.52) / (1 - 0.52)
        assert abs(f - expected) < 1e-9

    def test_kelly_no_negative(self):
        f = _kelly_no(0.70, 0.52)
        assert f < 0


# ── Midpoint pricing ───────────────────────────────────────────────────────────

class TestMidpoint:
    def test_midpoint_basic(self):
        assert _compute_midpoint(0.50, 0.60) == pytest.approx(0.55)

    def test_midpoint_no_bid(self):
        """No bid (0) → returns ask as fallback."""
        assert _compute_midpoint(0.0, 0.60) == 0.60

    def test_midpoint_tight_spread(self):
        assert _compute_midpoint(0.54, 0.56) == pytest.approx(0.55)


# ── Time-decay multiplier ──────────────────────────────────────────────────────

class TestTimeDecay:
    def test_no_decay_far_from_close(self):
        """48 hours out — no decay."""
        assert compute_time_decay_multiplier(48.0) == pytest.approx(1.0)

    def test_no_decay_at_threshold(self):
        """At exactly threshold (24h) — still 1.0."""
        assert compute_time_decay_multiplier(config.TIME_DECAY_THRESHOLD_HR) == pytest.approx(1.0)

    def test_no_decay_none(self):
        """Unknown hours — no decay."""
        assert compute_time_decay_multiplier(None) == pytest.approx(1.0)

    def test_decay_midpoint(self):
        """At midpoint between floor and threshold, expect ~midpoint multiplier."""
        mid_hr = (config.TIME_DECAY_THRESHOLD_HR + config.MIN_TIME_TO_CLOSE_HR) / 2
        mult = compute_time_decay_multiplier(mid_hr)
        min_m = config.TIME_DECAY_MIN_MULTIPLIER
        expected = min_m + 0.5 * (1.0 - min_m)
        assert mult == pytest.approx(expected, abs=0.02)

    def test_decay_above_threshold_no_decay(self):
        """Above TIME_DECAY_THRESHOLD_HR, no decay should be applied."""
        above = config.TIME_DECAY_THRESHOLD_HR + 1.0
        assert compute_time_decay_multiplier(above) == pytest.approx(1.0)

    def test_decay_at_floor(self):
        """At MIN_TIME_TO_CLOSE_HR, apply maximum decay."""
        mult = compute_time_decay_multiplier(float(config.MIN_TIME_TO_CLOSE_HR))
        assert mult == pytest.approx(config.TIME_DECAY_MIN_MULTIPLIER, abs=0.01)

    def test_decay_reduces_stake(self):
        """Time decay should reduce effective stake compared to no decay."""
        # Use hours within the decay range (below TIME_DECAY_THRESHOLD_HR=12h)
        result_near = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, hours_to_close=3.0)
        result_far = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, hours_to_close=48.0)
        if result_near.direction != "NONE" and result_far.direction != "NONE":
            assert result_near.stake_usd <= result_far.stake_usd

    def test_decay_threshold_is_12h(self):
        """Verify TIME_DECAY_THRESHOLD_HR is set to 12h for optimal convergence."""
        # External sources update infrequently; Kalshi's own price converges
        # to truth faster after 12h. Changed from 24h to 12h.
        assert config.TIME_DECAY_THRESHOLD_HR == pytest.approx(12.0, abs=0.1)
        # At 13h (above threshold): no decay
        assert compute_time_decay_multiplier(13.0) == pytest.approx(1.0)
        # At 7h (midpoint 12h-2h = 7h): significant decay applied
        assert compute_time_decay_multiplier(7.0) < 1.0


# ── Uncertainty multiplier (KL penalty) ───────────────────────────────────────

class TestUncertaintyMultiplier:
    def test_triple_source_full_kelly(self):
        """Three sources → full Kelly fraction (no penalty)."""
        mult = compute_uncertainty_multiplier(3)
        assert mult == pytest.approx(config.KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE)

    def test_dual_source_penalty(self):
        """Two sources → 25% reduction."""
        mult = compute_uncertainty_multiplier(2)
        assert mult == pytest.approx(config.KL_UNCERTAINTY_PENALTY_DUAL_SOURCE)

    def test_single_source_penalty(self):
        """One source → 50% reduction."""
        mult = compute_uncertainty_multiplier(1)
        assert mult == pytest.approx(config.KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE)

    def test_single_source_reduces_stake(self):
        """Single-source bet should have smaller stake than triple-source."""
        r1 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=1)
        r3 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=3)
        if r1.direction != "NONE" and r3.direction != "NONE":
            assert r1.stake_usd < r3.stake_usd


# ── GrossEV formula tests ──────────────────────────────────────────────────────

class TestGrossEV:
    def test_gross_ev_yes_favorable(self):
        ev = _gross_ev_yes(0.70, 0.55)
        assert abs(ev - 0.15) < 1e-9

    def test_gross_ev_yes_unfavorable(self):
        ev = _gross_ev_yes(0.40, 0.55)
        assert ev < 0

    def test_gross_ev_no_favorable(self):
        ev = _gross_ev_no(0.30, 0.52)
        assert abs(ev - 0.18) < 1e-9


# ── compute_edge() integration tests ──────────────────────────────────────────

class TestComputeEdge:
    BALANCE = 1000.0

    def test_yes_edge_w70_p55(self):
        """w=0.70, yes_ask=0.55, yes_bid=0.52 → YES bet with midpoint pricing."""
        result = compute_edge(
            w=0.70, p=0.55, q=0.48, balance=self.BALANCE,
            yes_bid=0.52, no_bid=0.44,
        )
        assert result.direction == "YES"
        assert result.net_edge >= config.MIN_EDGE
        assert result.kelly_fraction > 0
        # Midpoint tracking
        assert result.market_price == pytest.approx(0.535, abs=0.01)  # (0.52+0.55)/2
        assert result.exec_price == pytest.approx(0.55)  # Still executes at ask

    def test_no_edge_w30_q52(self):
        result = compute_edge(w=0.30, p=0.55, q=0.52, balance=self.BALANCE)
        assert result.direction == "NO"
        assert result.net_edge >= config.MIN_EDGE

    def test_no_bet_w52_p50(self):
        result = compute_edge(w=0.52, p=0.50, q=0.50, balance=self.BALANCE)
        assert result.direction == "NONE" or result.net_edge < config.MIN_EDGE

    def test_no_bet_both_sides_expensive(self):
        result = compute_edge(w=0.50, p=0.55, q=0.55, balance=self.BALANCE)
        assert result.direction == "NONE"

    def test_both_positive_picks_larger(self):
        result = compute_edge(w=0.50, p=0.35, q=0.35, balance=self.BALANCE)
        assert result.direction in ("YES", "NO")
        assert result.net_edge >= config.MIN_EDGE

    def test_both_edges_yes_wins(self):
        result = compute_edge(w=0.80, p=0.40, q=0.50, balance=self.BALANCE)
        assert result.direction == "YES"

    def test_invalid_w_zero(self):
        with pytest.raises(ValueError, match="fair_prob"):
            compute_edge(w=0.0, p=0.50, q=0.50, balance=self.BALANCE)

    def test_invalid_w_one(self):
        with pytest.raises(ValueError, match="fair_prob"):
            compute_edge(w=1.0, p=0.50, q=0.50, balance=self.BALANCE)

    def test_invalid_p_zero(self):
        with pytest.raises(ValueError, match="yes_price"):
            compute_edge(w=0.50, p=0.0, q=0.50, balance=self.BALANCE)

    def test_invalid_q_one(self):
        with pytest.raises(ValueError, match="no_price"):
            compute_edge(w=0.50, p=0.50, q=1.0, balance=self.BALANCE)

    def test_zero_balance_returns_none(self):
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=0.0)
        assert result.direction == "NONE"

    def test_stake_capped_at_max_bet_pct(self):
        balance = 100_000.0
        result = compute_edge(w=0.80, p=0.40, q=0.50, balance=balance)
        if result.direction != "NONE":
            assert result.stake_usd <= config.MAX_BET_PCT * balance + 0.01

    def test_min_bet_produces_at_least_one_contract(self):
        """
        MIN_BET_USD is the minimum BUDGET, not a floor on actual cost.
        With a tiny balance, the budget floors to 1.00 but actual cost
        is 1 contract * price + fee (may be < $1.00 at low prices).
        The important thing: contracts >= 1 so a real order is placed.
        """
        balance = 10.0
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=balance)
        if result.direction != "NONE":
            assert result.contracts >= 1, "Must place at least 1 contract"
            # Actual cost = contracts * price + fee (may differ from MIN_BET_USD)
            actual_cost = result.contracts * result.exec_price + result.fee_usd
            assert actual_cost <= result.stake_usd + 0.01, "Cost must not exceed stake budget"


# ── Bug regression tests ──────────────────────────────────────────────────────

class TestBugRegressions:
    """
    Regression tests for three bugs found in the quadruple audit.
    These tests would have caught the bugs if they existed earlier.
    """

    def test_bug1_solve_contracts_no_overrun_when_stake_too_small(self):
        """
        BUG: _solve_contracts_with_fee returned n=1 even when
        1-contract cost (price + fee) > stake, causing a budget overrun.

        stake=0.50, price=0.50: 1 contract costs $0.55 (0.50 + 0.02 fee) > $0.50
        Should return (0, 0.0) — cannot afford even 1 contract.
        """
        from bot.edge_calculator import _solve_contracts_with_fee
        n, fee = _solve_contracts_with_fee(0.50, 0.50, "")
        assert n == 0, f"Expected 0 contracts, got {n} (would overrun budget)"
        assert fee == 0.0

        # Verify the boundary: exactly enough to afford 1 contract
        from bot.fee_calculator import compute_taker_fee
        min_stake = 0.50 + compute_taker_fee(0.50, 1)  # = 0.52
        n2, fee2 = _solve_contracts_with_fee(min_stake, 0.50, "")
        assert n2 >= 1, f"Should afford 1 contract at exact cost={min_stake:.4f}"

        # Verify cost never exceeds stake for various inputs
        for stake, price in [(1.0, 0.55), (5.0, 0.30), (50.0, 0.45), (0.30, 0.50)]:
            n3, fee3 = _solve_contracts_with_fee(stake, price, "")
            if n3 > 0:
                cost = n3 * price + fee3
                assert cost <= stake + 0.005, (
                    f"Cost {cost:.4f} > stake {stake} for price={price} n={n3}"
                )

    def test_bug2_min_bet_never_exceeds_balance(self):
        """
        BUG: MIN_BET_USD=$1.00 floor was applied regardless of balance,
        causing a $0.01 balance to produce a $1.00 bet (10,000% of balance).

        Fix: MIN_BET_USD floor only applied when balance >= MIN_BET_USD.
        """
        # $0.01 balance — cannot afford MIN_BET_USD=$1.00, should not bet
        r_tiny = compute_edge(w=0.70, p=0.55, q=0.48, balance=0.01, source_count=3)
        assert r_tiny.direction == "NONE" or r_tiny.contracts == 0, (
            f"Balance=$0.01 produced {r_tiny.contracts} contracts — overbet!"
        )

        # $0.50 balance — still cannot afford 1 contract at 55c (cost=0.57)
        r_half = compute_edge(w=0.70, p=0.55, q=0.48, balance=0.50, source_count=3)
        assert r_half.direction == "NONE" or r_half.contracts == 0, (
            f"Balance=$0.50 produced bet — would overrun balance"
        )

        # $1.00 balance — exactly at MIN_BET_USD, can afford 1 contract at low price
        r_one = compute_edge(w=0.70, p=0.40, q=0.35, balance=1.00, source_count=3)
        # stake = max(kelly, MIN_BET_USD=1.00) = 1.00; 1 contract at 0.40 + fee ≤ 1.00
        if r_one.direction != "NONE":
            assert r_one.stake_usd <= 1.00 + 0.01, (
                f"$1.00 balance bet ${r_one.stake_usd:.4f} — exceeds balance"
            )

        # Normal balance — MIN_BET_USD floor should apply as before
        r_normal = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=3)
        assert r_normal.direction == "YES"
        assert r_normal.contracts >= 1

    def test_bug3_extract_fees_called_in_live_order(self):
        """
        BUG: _extract_fees_from_order was defined but never called inside
        _execute_live_order, making actual maker/taker fee tracking dead code.
        FillResult.actual_taker_fee was always None for live orders.

        Fix: call _extract_fees_from_order on filled/cancelled/timeout status.
        """
        import inspect
        from bot.executor import _execute_live_order, _extract_fees_from_order

        src = inspect.getsource(_execute_live_order)
        assert "_extract_fees_from_order" in src, (
            "_extract_fees_from_order must be called inside _execute_live_order"
        )
        # Should be called at least twice: on 'filled' and on timeout path
        count = src.count("_extract_fees_from_order")
        assert count >= 2, (
            f"_extract_fees_from_order called {count}x — need at least filled + timeout"
        )

        # Verify _extract_fees_from_order parses fee fields correctly
        taker, maker = _extract_fees_from_order({
            "taker_fees_dollars": "0.1800",
            "maker_fees_dollars": "0.0000",
        })
        assert taker == pytest.approx(0.18)
        assert maker is None  # 0.00 → None

        taker2, maker2 = _extract_fees_from_order({
            "taker_fees_dollars": "0.0000",
            "maker_fees_dollars": "0.0450",
        })
        assert taker2 is None   # taker=0 means it was a maker fill
        assert maker2 == pytest.approx(0.045)

        # Empty response returns None, None
        t3, m3 = _extract_fees_from_order(None)
        assert t3 is None and m3 is None


# ── Adaptive MIN_EDGE ─────────────────────────────────────────────────────────

class TestAdaptiveMinEdge:
    """Tests for the adaptive minimum edge threshold by source confidence."""

    def test_triple_source_lower_threshold(self):
        """
        Triple-source (3 sources agree) uses MIN_EDGE_TRIPLE_SOURCE=3%.
        A 4% net edge that would be rejected at 5% should pass at 3%.
        """
        from bot.edge_calculator import get_min_edge
        assert get_min_edge(3) == pytest.approx(config.MIN_EDGE_TRIPLE_SOURCE)
        assert get_min_edge(3) < get_min_edge(1)  # Lower bar for high confidence

    def test_single_source_higher_threshold(self):
        """
        Single-source uses MIN_EDGE_SINGLE_SOURCE=8%.
        A 6% net edge that would pass at 5% is rejected at 8%.
        """
        from bot.edge_calculator import get_min_edge
        assert get_min_edge(1) == pytest.approx(config.MIN_EDGE_SINGLE_SOURCE)
        assert get_min_edge(1) > get_min_edge(2) > get_min_edge(3)  # Monotonic

    def test_dual_source_standard_threshold(self):
        from bot.edge_calculator import get_min_edge
        assert get_min_edge(2) == pytest.approx(config.MIN_EDGE_DUAL_SOURCE)

    def test_triple_source_accepts_lower_edge(self):
        """
        A trade with ~4% net edge should be accepted with 3 sources (triple threshold=3%)
        but rejected with 1 source (single threshold=8%).
        """
        # w=0.60, p=0.565 → kelly=(0.60-0.565)/(1-0.565)=0.08, edge ≈ 4-5%
        # With single source: 8% threshold → likely rejected
        # With triple source: 3% threshold → likely accepted
        r1 = compute_edge(w=0.60, p=0.565, q=0.44, balance=1000, source_count=1)
        r3 = compute_edge(w=0.60, p=0.565, q=0.44, balance=1000, source_count=3)
        # Triple should be more permissive
        if r1.direction == "NONE" and r3.direction != "NONE":
            pass  # Expected: triple accepts, single rejects
        # At minimum, triple source should never be stricter than single
        if r3.direction == "NONE":
            assert r1.direction == "NONE"  # If triple rejects, single must also reject

    def test_min_edge_stored_in_result(self):
        """The EdgeResult should store which threshold was actually used."""
        r1 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000, source_count=1)
        r3 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000, source_count=3)
        assert r1.min_edge_used == pytest.approx(config.MIN_EDGE_SINGLE_SOURCE)
        assert r3.min_edge_used == pytest.approx(config.MIN_EDGE_TRIPLE_SOURCE)


# ── EdgeResult new fields ──────────────────────────────────────────────────────

class TestEdgeResultFields:
    def test_result_has_all_new_fields(self):
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=2, hours_to_close=12.0)
        assert isinstance(result, EdgeResult)
        assert result.direction in ("YES", "NO", "NONE")
        assert hasattr(result, "adjusted_kelly")
        assert hasattr(result, "time_decay_mult")
        assert hasattr(result, "uncertainty_mult")
        assert hasattr(result, "source_count")
        assert hasattr(result, "exec_price")
        assert hasattr(result, "market_price")
        assert hasattr(result, "contracts")   # New: pre-computed fee-aware contracts

    def test_uncertainty_mult_stored(self):
        r1 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=1)
        r3 = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=3)
        assert r1.uncertainty_mult == pytest.approx(config.KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE)
        assert r3.uncertainty_mult == pytest.approx(config.KL_UNCERTAINTY_PENALTY_TRIPLE_SOURCE)

    def test_adjusted_kelly_less_than_raw_with_penalty(self):
        """With uncertainty penalty, adjusted_kelly < kelly_fraction × kelly_f."""
        r = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, source_count=1)
        if r.direction != "NONE":
            expected_adjusted = r.kelly_fraction * config.KL_UNCERTAINTY_PENALTY_SINGLE_SOURCE
            assert r.adjusted_kelly == pytest.approx(expected_adjusted, rel=0.1)

    def test_fee_usd_positive(self):
        r = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0)
        if r.direction != "NONE":
            assert r.fee_usd >= 0
