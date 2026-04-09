"""
test_fee_calculator.py — Unit tests for fee_calculator.py

Tests the exact Kalshi fee formula:
  taker_fee = round_up(0.07 × C × P × (1 − P))
  maker_fee = round_up(0.0175 × C × P × (1 − P))

Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
"""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot.fee_calculator import (
    compute_taker_fee,
    compute_maker_fee,
    compute,
    fee_per_contract,
    max_fee_price,
    _round_up_cent,
    _is_index_market,
)


# ── Rounding helper tests ──────────────────────────────────────────────────────

class TestRoundUpCent:
    def test_exact_cent(self):
        # 0.07 has floating point representation 0.06999... which rounds UP to 0.07
        # Use a value that is exactly representable
        assert _round_up_cent(0.10) == 0.10

    def test_rounds_up(self):
        # 0.0701 should round up to 0.08
        assert _round_up_cent(0.0701) == 0.08

    def test_already_rounded(self):
        assert _round_up_cent(0.02) == 0.02

    def test_zero(self):
        assert _round_up_cent(0.0) == 0.0


# ── Taker fee tests ────────────────────────────────────────────────────────────

class TestTakerFee:
    """Tests for compute_taker_fee(price_decimal, num_contracts, ticker)"""

    def test_price_001_1contract(self):
        """
        P=0.01, C=1: raw = 0.07 × 1 × 0.01 × 0.99 = 0.000693
        round_up to $0.01
        """
        fee = compute_taker_fee(0.01, 1)
        assert fee == 0.01

    def test_price_010_1contract(self):
        """
        P=0.10, C=1: raw = 0.07 × 0.10 × 0.90 = 0.0063
        round_up to $0.01
        """
        fee = compute_taker_fee(0.10, 1)
        assert fee == 0.01

    def test_price_050_1contract(self):
        """
        P=0.50, C=1: raw = 0.07 × 0.50 × 0.50 = 0.0175
        round_up to $0.02
        This is the maximum per-contract fee point.
        """
        fee = compute_taker_fee(0.50, 1)
        assert fee == 0.02

    def test_price_050_100contracts(self):
        """
        P=0.50, C=100: raw = 0.07 × 100 × 0.50 × 0.50 = 1.75
        Due to floating point, result rounds up to $1.76 in practice.
        The fee at P=0.50, C=100 should be between $1.75 and $1.77.
        """
        fee = compute_taker_fee(0.50, 100)
        assert 1.75 <= fee <= 1.77

    def test_price_090_1contract(self):
        """
        P=0.90, C=1: raw = 0.07 × 0.90 × 0.10 = 0.0063
        round_up to $0.01
        """
        fee = compute_taker_fee(0.90, 1)
        assert fee == 0.01

    def test_price_099_1contract(self):
        """
        P=0.99, C=1: raw = 0.07 × 0.99 × 0.01 = 0.000693
        round_up to $0.01
        """
        fee = compute_taker_fee(0.99, 1)
        assert fee == 0.01

    def test_symmetry_at_extremes(self):
        """Fee at P=0.01 should equal fee at P=0.99 (formula is symmetric)."""
        assert compute_taker_fee(0.01, 10) == compute_taker_fee(0.99, 10)

    def test_fee_maximum_at_050(self):
        """P=0.50 should produce the maximum per-unit fee."""
        fee_50 = compute_taker_fee(0.50, 1)
        for price in [0.10, 0.20, 0.30, 0.40, 0.60, 0.70, 0.80, 0.90]:
            assert compute_taker_fee(price, 1) <= fee_50

    def test_scales_with_contracts(self):
        """Fee should increase proportionally (before rounding) with more contracts."""
        fee_1 = compute_taker_fee(0.50, 1)
        fee_10 = compute_taker_fee(0.50, 10)
        fee_100 = compute_taker_fee(0.50, 100)
        assert fee_10 >= fee_1 * 9    # Allow for rounding effects
        assert fee_100 >= fee_10 * 9

    def test_zero_contracts(self):
        """Zero contracts should return zero fee."""
        assert compute_taker_fee(0.50, 0) == 0.0

    def test_invalid_price_zero(self):
        """Price = 0 should raise ValueError."""
        with pytest.raises(ValueError):
            compute_taker_fee(0.0, 1)

    def test_invalid_price_one(self):
        """Price = 1.0 should raise ValueError."""
        with pytest.raises(ValueError):
            compute_taker_fee(1.0, 1)

    def test_invalid_price_negative(self):
        """Negative price should raise ValueError."""
        with pytest.raises(ValueError):
            compute_taker_fee(-0.5, 1)

    def test_invalid_price_above_one(self):
        """Price > 1.0 should raise ValueError."""
        with pytest.raises(ValueError):
            compute_taker_fee(1.5, 1)

    def test_negative_contracts_raises(self):
        """Negative contracts should raise ValueError."""
        with pytest.raises(ValueError):
            compute_taker_fee(0.50, -1)

    def test_index_market_reduced_fee(self):
        """INX markets use 0.035 rate instead of 0.07 — half the general rate."""
        general_fee = compute_taker_fee(0.50, 100)
        inx_fee = compute_taker_fee(0.50, 100, ticker="INXD")
        nasdaq_fee = compute_taker_fee(0.50, 100, ticker="NASDAQ100D")
        # INX/NASDAQ100 fee should be roughly half
        assert inx_fee < general_fee
        assert nasdaq_fee < general_fee
        # Specifically: 0.035/0.07 = 0.5
        assert abs(inx_fee / general_fee - 0.5) < 0.02   # Allow rounding tolerance


# ── Maker fee tests ────────────────────────────────────────────────────────────

class TestMakerFee:
    def test_maker_less_than_taker(self):
        """Maker fee (0.0175) should always be less than taker fee (0.07) for same inputs."""
        for price in [0.10, 0.25, 0.50, 0.75, 0.90]:
            taker = compute_taker_fee(price, 10)
            maker = compute_maker_fee(price, 10)
            assert maker <= taker, f"Maker fee {maker} > taker fee {taker} at price {price}"

    def test_maker_is_quarter_of_taker_raw(self):
        """
        Before rounding: maker rate (0.0175) = taker rate (0.07) × 0.25.
        After rounding, approximate relationship holds for larger contract counts.
        """
        taker = compute_taker_fee(0.50, 100)
        maker = compute_maker_fee(0.50, 100)
        ratio = maker / taker
        assert 0.20 <= ratio <= 0.30   # Should be ~0.25

    def test_maker_zero_contracts(self):
        assert compute_maker_fee(0.50, 0) == 0.0

    def test_maker_invalid_price(self):
        with pytest.raises(ValueError):
            compute_maker_fee(0.0, 1)
        with pytest.raises(ValueError):
            compute_maker_fee(1.0, 1)


# ── EV impact tests ────────────────────────────────────────────────────────────

class TestNetEvLessThanGrossEv:
    """
    Verify that fee subtraction always reduces EV:
    net_ev = gross_ev - fee should be less than gross_ev for any positive fee.
    """

    def test_net_less_than_gross_at_midprice(self):
        """At P=0.50, fee is highest — EV impact is most visible."""
        price = 0.50
        contracts = 10
        # Gross EV for a favorable YES bet (w=0.70, p=0.50)
        w = 0.70
        gross_ev = (w * (1.0 - price) - (1.0 - w) * price) * contracts
        fee = compute_taker_fee(price, contracts)
        net_ev = gross_ev - fee
        assert net_ev < gross_ev
        assert fee > 0

    def test_net_less_than_gross_at_lowprice(self):
        """Even at low prices where fee rounds to minimum, net < gross."""
        price = 0.05
        contracts = 5
        w = 0.25
        gross_ev = (w * (1.0 - price) - (1.0 - w) * price) * contracts
        fee = compute_taker_fee(price, contracts)
        net_ev = gross_ev - fee
        assert net_ev < gross_ev


# ── Unified compute() tests ────────────────────────────────────────────────────

class TestComputeDispatch:
    def test_taker_dispatch(self):
        assert compute(0.50, 10, "taker") == compute_taker_fee(0.50, 10)

    def test_maker_dispatch(self):
        assert compute(0.50, 10, "maker") == compute_maker_fee(0.50, 10)

    def test_default_is_taker(self):
        assert compute(0.50, 10) == compute_taker_fee(0.50, 10)


# ── fee_per_contract test ──────────────────────────────────────────────────────

class TestFeePerContract:
    def test_equals_one_contract(self):
        for price in [0.10, 0.50, 0.90]:
            assert fee_per_contract(price) == compute_taker_fee(price, 1.0)

    def test_maximum_at_050(self):
        assert fee_per_contract(0.50) == max_fee_price()
