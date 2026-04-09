"""
test_edge_calculator.py — Unit tests for edge_calculator.py

Tests:
  - YES edge: w=0.70, p=0.55 → verify f and stake
  - NO edge: w=0.30, q=0.52 → verify f and stake
  - No-bet: w=0.52, p=0.50 → below MIN_EDGE
  - Both edges positive → picks larger
  - Invalid probability → raises ValueError
"""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import config
from bot.edge_calculator import (
    compute_edge,
    EdgeResult,
    _gross_ev_yes,
    _gross_ev_no,
    _kelly_yes,
    _kelly_no,
)
from bot.fee_calculator import compute_taker_fee


# ── Kelly formula unit tests ───────────────────────────────────────────────────

class TestKellyFormulas:
    def test_kelly_yes_positive(self):
        """w=0.70, p=0.55: f_yes = (0.70 - 0.55) / (1 - 0.55) = 0.15 / 0.45 ≈ 0.333"""
        f = _kelly_yes(0.70, 0.55)
        assert abs(f - (0.70 - 0.55) / (1 - 0.55)) < 1e-9

    def test_kelly_yes_negative(self):
        """w < p → f_yes should be negative (no bet)."""
        f = _kelly_yes(0.40, 0.55)
        assert f < 0

    def test_kelly_no_positive(self):
        """w=0.30, q=0.52: f_no = ((1-0.30) - 0.52) / (1 - 0.52) = 0.18 / 0.48 = 0.375"""
        f = _kelly_no(0.30, 0.52)
        expected = ((1 - 0.30) - 0.52) / (1 - 0.52)
        assert abs(f - expected) < 1e-9

    def test_kelly_no_negative(self):
        """When (1-w) < q, no-bet signal."""
        f = _kelly_no(0.70, 0.52)
        assert f < 0


# ── GrossEV formula tests ──────────────────────────────────────────────────────

class TestGrossEV:
    def test_gross_ev_yes_favorable(self):
        """w=0.70, p=0.55: EV = 0.70*(0.45) - 0.30*(0.55) = 0.315 - 0.165 = 0.15"""
        ev = _gross_ev_yes(0.70, 0.55)
        assert abs(ev - 0.15) < 1e-9

    def test_gross_ev_yes_unfavorable(self):
        """w=0.40, p=0.55: should be negative."""
        ev = _gross_ev_yes(0.40, 0.55)
        assert ev < 0

    def test_gross_ev_no_favorable(self):
        """w=0.30, q=0.52: EV_no = 0.70*(0.48) - 0.30*(0.52) = 0.336 - 0.156 = 0.18"""
        ev = _gross_ev_no(0.30, 0.52)
        assert abs(ev - 0.18) < 1e-9

    def test_gross_ev_no_unfavorable(self):
        """w=0.60, q=0.52: no edge on NO side."""
        ev = _gross_ev_no(0.60, 0.52)
        assert ev < 0


# ── compute_edge() integration tests ──────────────────────────────────────────

class TestComputeEdge:
    BALANCE = 1000.0   # $1,000 test balance

    def test_yes_edge_w70_p55(self):
        """
        w=0.70, p=0.55, q=0.48 → YES bet expected.
        Kelly: f_yes = (0.70 - 0.55) / 0.45 ≈ 0.333
        Quarter-Kelly stake ≈ 0.25 × 0.333 × 1000 = $83.3
        Capped at MAX_BET_PCT=5% × $1000 = $50
        """
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=self.BALANCE)
        assert result.direction == "YES"
        assert result.net_edge >= config.MIN_EDGE
        assert result.kelly_fraction > 0

        # Stake should be capped at MAX_BET_PCT
        max_stake = config.MAX_BET_PCT * self.BALANCE
        assert result.stake_usd <= max_stake + 0.01

        # Fee must be > 0
        assert result.fee_usd >= 0

        # Gross edge > net edge (fee reduces it)
        assert result.gross_edge >= result.net_edge

    def test_no_edge_w30_q52(self):
        """
        w=0.30, p=0.55, q=0.52 → NO bet expected.
        Kelly_no = ((1-0.30) - 0.52) / (1-0.52) = 0.18/0.48 = 0.375
        """
        result = compute_edge(w=0.30, p=0.55, q=0.52, balance=self.BALANCE)
        assert result.direction == "NO"
        assert result.net_edge >= config.MIN_EDGE
        assert result.kelly_fraction > 0

    def test_no_bet_w52_p50(self):
        """
        w=0.52, p=0.50, q=0.50 → Very small edge, should be below MIN_EDGE after fees.
        Kelly_yes = (0.52 - 0.50) / 0.50 = 0.04 = 4% < MIN_EDGE 5%
        """
        result = compute_edge(w=0.52, p=0.50, q=0.50, balance=self.BALANCE)
        # Net edge after fees should be below threshold
        assert result.direction == "NONE" or result.net_edge < config.MIN_EDGE

    def test_no_bet_unfavorable(self):
        """
        w=0.45, p=0.55 → YES is overpriced (no YES edge).
        BUT NO at q=0.48: kelly_no = ((1-0.45) - 0.48) / (1-0.48) = 0.07/0.52 = 0.135
        That's ~13.5% which exceeds MIN_EDGE — so NO has edge.
        Use a scenario where BOTH sides are genuinely unfavorable.
        """
        # w=0.50, p=0.55, q=0.55: both sides expensive
        result = compute_edge(w=0.50, p=0.55, q=0.55, balance=self.BALANCE)
        # YES kelly: (0.50-0.55)/(1-0.55) = -0.05/0.45 < 0 → no bet
        # NO kelly: ((1-0.50)-0.55)/(1-0.55) = -0.05/0.45 < 0 → no bet
        assert result.direction == "NONE"

    def test_both_positive_picks_larger(self):
        """
        If both YES and NO show positive edge, the one with higher net_edge wins.
        This is a synthetic test — create a scenario where both sides are underpriced.
        """
        # w=0.50, both YES (p=0.35) and NO (q=0.35) are cheap
        # YES: (0.50-0.35)/(1-0.35)=0.231, NO: (0.50-0.35)/(1-0.35)=0.231 → equal → YES by tie order
        result = compute_edge(w=0.50, p=0.35, q=0.35, balance=self.BALANCE)
        assert result.direction in ("YES", "NO")
        assert result.net_edge >= config.MIN_EDGE

    def test_both_edges_picks_higher(self):
        """When one edge clearly dominates, that direction is chosen."""
        # YES has much bigger edge: w=0.80, p=0.40, q=0.50
        # YES kelly = (0.80-0.40)/0.60 = 0.667 >> NO kelly = (0.20-0.50)/(0.50) < 0
        result = compute_edge(w=0.80, p=0.40, q=0.50, balance=self.BALANCE)
        assert result.direction == "YES"
        # YES should win decisively
        assert result.kelly_fraction > 0.3

    def test_invalid_w_zero(self):
        """w=0 is invalid — must raise ValueError."""
        with pytest.raises(ValueError, match="fair_prob"):
            compute_edge(w=0.0, p=0.50, q=0.50, balance=self.BALANCE)

    def test_invalid_w_one(self):
        """w=1.0 is invalid — must raise ValueError."""
        with pytest.raises(ValueError, match="fair_prob"):
            compute_edge(w=1.0, p=0.50, q=0.50, balance=self.BALANCE)

    def test_invalid_p_zero(self):
        with pytest.raises(ValueError, match="yes_price"):
            compute_edge(w=0.50, p=0.0, q=0.50, balance=self.BALANCE)

    def test_invalid_q_one(self):
        with pytest.raises(ValueError, match="no_price"):
            compute_edge(w=0.50, p=0.50, q=1.0, balance=self.BALANCE)

    def test_zero_balance_returns_none(self):
        """With zero balance, no bet should be placed."""
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=0.0)
        assert result.direction == "NONE"

    def test_stake_capped_at_max_bet_pct(self):
        """Stake should never exceed MAX_BET_PCT × balance."""
        balance = 100_000.0   # Large balance
        result = compute_edge(w=0.80, p=0.40, q=0.50, balance=balance)
        if result.direction != "NONE":
            assert result.stake_usd <= config.MAX_BET_PCT * balance + 0.01

    def test_stake_floored_at_min_bet(self):
        """Stake should be at least MIN_BET_USD."""
        balance = 10.0   # Very small balance → Kelly might produce < $1
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=balance)
        if result.direction != "NONE":
            assert result.stake_usd >= config.MIN_BET_USD


# ── Edge result fields ─────────────────────────────────────────────────────────

class TestEdgeResultFields:
    def test_result_has_all_fields(self):
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=1000.0)
        assert isinstance(result, EdgeResult)
        assert result.direction in ("YES", "NO", "NONE")
        assert isinstance(result.gross_edge, float)
        assert isinstance(result.net_edge, float)
        assert isinstance(result.kelly_fraction, float)
        assert isinstance(result.stake_usd, float)
        assert isinstance(result.fair_prob, float)
        assert isinstance(result.market_price, float)
        assert isinstance(result.fee_usd, float)

    def test_fee_usd_matches_fee_calculator(self):
        """fee_usd in result should match fee_calculator for same inputs."""
        balance = 1000.0
        result = compute_edge(w=0.70, p=0.55, q=0.48, balance=balance)
        if result.direction == "YES":
            contracts = math.floor(result.stake_usd / 0.55)
            expected_fee = compute_taker_fee(0.55, contracts)
            # Allow small tolerance for floating point
            assert abs(result.fee_usd - expected_fee) < 0.02
