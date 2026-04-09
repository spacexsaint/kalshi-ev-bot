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

    def test_decay_at_floor(self):
        """At MIN_TIME_TO_CLOSE_HR, apply maximum decay."""
        mult = compute_time_decay_multiplier(float(config.MIN_TIME_TO_CLOSE_HR))
        assert mult == pytest.approx(config.TIME_DECAY_MIN_MULTIPLIER, abs=0.01)

    def test_decay_reduces_stake(self):
        """Time decay should reduce effective stake compared to no decay."""
        result_near = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, hours_to_close=3.0)
        result_far = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0, hours_to_close=48.0)
        if result_near.direction != "NONE" and result_far.direction != "NONE":
            assert result_near.stake_usd <= result_far.stake_usd


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

    def test_stake_floored_at_min_bet(self):
        balance = 10.0
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=balance)
        if result.direction != "NONE":
            assert result.stake_usd >= config.MIN_BET_USD


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
