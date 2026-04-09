"""
test_backtest_brier.py — Regression test for Brier score using fair_prob (not entry_price).

BUG (pre-fix): _compute_backtest_brier used trade.entry_price (market ask) as the
probability estimate instead of trade.fair_prob (source-aggregated probability).
This measured *market* calibration, not *source* calibration — defeating the
purpose of the Brier score in evaluating our external probability sources.

FIX: BacktestTrade now stores fair_prob; Brier score computation uses it.
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backtest import BacktestTrade, BacktestResult, _compute_backtest_brier


class TestBrierScoreFairProb:
    """Verify Brier score uses fair_prob, not entry_price."""

    def _make_trade(self, fair_prob: float, entry_price: float, resolved_yes: bool) -> BacktestTrade:
        return BacktestTrade(
            ticker="TEST-001",
            direction="YES",
            entry_price=entry_price,
            fair_prob=fair_prob,
            contracts=1.0,
            stake_usd=1.0,
            fee_usd=0.01,
            net_edge=0.05,
            gross_edge=0.06,
            resolved_yes=resolved_yes,
            pnl_usd=0.10,
            hold_hours=12.0,
        )

    def test_brier_uses_fair_prob_not_entry_price(self):
        """
        Regression: if fair_prob=0.80 and entry_price=0.60, and outcome=YES (1.0):
        - Correct Brier (using fair_prob):   (0.80 - 1.0)^2 = 0.04
        - Wrong Brier (using entry_price):   (0.60 - 1.0)^2 = 0.16
        """
        trade = self._make_trade(fair_prob=0.80, entry_price=0.60, resolved_yes=True)
        result = BacktestResult(
            trades=[trade],
            starting_balance=100.0,
            ending_balance=100.10,
            equity_curve=[100.0, 100.10],
            timestamps=[0, 12],
        )
        brier = _compute_backtest_brier(result, [])
        expected = (0.80 - 1.0) ** 2  # = 0.04
        assert abs(brier - expected) < 1e-9, (
            f"Brier={brier:.6f}, expected={expected:.6f}. "
            f"If brier≈0.16, fair_prob was not used (entry_price was used instead)."
        )

    def test_brier_perfect_calibration(self):
        """fair_prob=1.0 should not be used (strict (0,1)), but 0.99 resolved YES → near 0."""
        trade = self._make_trade(fair_prob=0.99, entry_price=0.95, resolved_yes=True)
        result = BacktestResult(
            trades=[trade],
            starting_balance=100.0,
            ending_balance=100.05,
            equity_curve=[100.0, 100.05],
            timestamps=[0, 12],
        )
        brier = _compute_backtest_brier(result, [])
        assert brier < 0.001  # (0.99 - 1.0)^2 = 0.0001

    def test_brier_no_trades_returns_nan(self):
        result = BacktestResult(
            trades=[],
            starting_balance=100.0,
            ending_balance=100.0,
            equity_curve=[100.0],
            timestamps=[0],
        )
        brier = _compute_backtest_brier(result, [])
        assert math.isnan(brier)

    def test_brier_multiple_trades_averaged(self):
        """Mean Brier across two trades."""
        t1 = self._make_trade(fair_prob=0.90, entry_price=0.85, resolved_yes=True)
        t2 = self._make_trade(fair_prob=0.30, entry_price=0.35, resolved_yes=False)
        result = BacktestResult(
            trades=[t1, t2],
            starting_balance=100.0,
            ending_balance=100.20,
            equity_curve=[100.0, 100.10, 100.20],
            timestamps=[0, 12, 24],
        )
        brier = _compute_backtest_brier(result, [])
        # (0.90 - 1.0)^2 = 0.01, (0.30 - 0.0)^2 = 0.09 → mean = 0.05
        expected = ((0.90 - 1.0) ** 2 + (0.30 - 0.0) ** 2) / 2
        assert abs(brier - expected) < 1e-9
