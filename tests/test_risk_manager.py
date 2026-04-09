"""
test_risk_manager.py — Unit tests for risk_manager.py

Tests:
  - can_trade() returns False when daily loss > 15%
  - can_trade() returns False when positions >= MAX_OPEN_POSITIONS
  - reset_daily() correctly resets state
  - Partial fills recorded correctly
"""

import sys
import os
import json
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import config


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_risk_manager(tmp_path, monkeypatch):
    """
    Isolate each test:
    - Use a temporary state file
    - Reset the _halted flag before each test
    - Reload state_manager and risk_manager modules cleanly
    """
    import bot.config as cfg
    state_path = str(tmp_path / "state.json")
    monkeypatch.setattr(cfg, "STATE_FILE", state_path)
    monkeypatch.setattr(cfg, "EVENTS_LOG", str(tmp_path / "events.jsonl"))

    # Reset module-level _halted flag
    import bot.risk_manager as rm
    rm._halted = False

    # Load fresh state
    import bot.state_manager as sm
    sm._state = sm._default_state()
    sm._state["daily_start_balance"] = 1000.0
    sm._state["daily_pnl"] = 0.0

    yield

    # Cleanup
    rm._halted = False


# ── can_trade() tests ──────────────────────────────────────────────────────────

class TestCanTrade:
    def test_can_trade_when_healthy(self):
        """With no losses and few positions, can_trade() should be True."""
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 0.0
        sm._state["open_positions"] = []
        assert rm.can_trade() is True

    def test_cannot_trade_when_loss_limit_hit(self):
        """
        Daily loss > 15% of start balance → can_trade() = False.
        Start balance = $1000, 15% = $150 loss threshold.
        """
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -151.0   # Exceeds $150 threshold

        assert rm.can_trade() is False

    def test_cannot_trade_at_exact_threshold(self):
        """At exactly -15% (= -$150.00), should halt."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -150.0

        result = rm.can_trade()
        assert result is False

    def test_can_trade_just_below_threshold(self):
        """At -14.9% loss, trading should still be allowed."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -149.0

        rm._halted = False   # Ensure not already tripped
        result = rm.can_trade()
        assert result is True

    def test_cannot_trade_when_positions_at_max(self):
        """
        When open_position_count >= MAX_OPEN_POSITIONS (10), cannot trade.
        """
        import bot.risk_manager as rm
        import bot.state_manager as sm

        # Fill up positions
        sm._state["open_positions"] = [
            {
                "ticker": f"MARKET-{i:03d}",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 10.0,
                "stake_usd": 50.0,
                "fair_prob_at_entry": 0.60,
                "net_edge_at_entry": 0.07,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-{i}",
            }
            for i in range(config.MAX_OPEN_POSITIONS)
        ]

        result = rm.can_trade()
        assert result is False

    def test_can_trade_one_below_position_cap(self):
        """With MAX_OPEN_POSITIONS - 1 open positions, should still be able to trade."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["open_positions"] = [
            {
                "ticker": f"MARKET-{i:03d}",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 10.0,
                "stake_usd": 50.0,
                "fair_prob_at_entry": 0.60,
                "net_edge_at_entry": 0.07,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-{i}",
            }
            for i in range(config.MAX_OPEN_POSITIONS - 1)
        ]

        rm._halted = False
        result = rm.can_trade()
        assert result is True

    def test_halted_flag_blocks_trading(self):
        """Once _halted is True, can_trade() returns False regardless of PnL."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = 100.0   # Profitable day
        rm._halted = True

        result = rm.can_trade()
        assert result is False

    def test_circuit_breaker_sets_halted_flag(self):
        """Triggering the loss limit should set _halted = True persistently."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -200.0

        rm.can_trade()   # Should trigger halt
        assert rm._halted is True

        # Subsequent call should also return False
        assert rm.can_trade() is False


# ── reset_daily() tests ────────────────────────────────────────────────────────

class TestResetDaily:
    def test_reset_clears_pnl(self):
        """reset_daily() should zero out daily PnL."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = -200.0
        rm._halted = True

        rm.reset_daily(current_balance_usd=800.0)

        assert sm.get_daily_pnl() == 0.0

    def test_reset_clears_halted_flag(self):
        """reset_daily() must clear the circuit breaker."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        rm._halted = True
        rm.reset_daily(current_balance_usd=1000.0)

        assert rm._halted is False

    def test_reset_updates_start_balance(self):
        """reset_daily() should set daily_start_balance to the new balance."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        rm.reset_daily(current_balance_usd=750.0)

        assert sm.get_daily_start_balance() == pytest.approx(750.0)

    def test_can_trade_after_reset(self):
        """After reset, can_trade() should return True if positions allow."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = -500.0
        rm._halted = True
        sm._state["open_positions"] = []

        rm.reset_daily(current_balance_usd=500.0)

        assert rm.can_trade() is True


# ── record_pnl() / record_fill() tests ────────────────────────────────────────

class TestRecordPnL:
    def test_record_positive_pnl(self):
        """Positive PnL should increase daily_pnl."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = 0.0
        rm.record_pnl(50.0)
        assert sm.get_daily_pnl() == pytest.approx(50.0)

    def test_record_negative_pnl(self):
        """Loss should decrease daily_pnl."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = 100.0
        rm.record_pnl(-30.0)
        assert sm.get_daily_pnl() == pytest.approx(70.0)

    def test_cumulative_pnl(self):
        """Multiple record_pnl calls should accumulate."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_pnl"] = 0.0
        rm.record_pnl(10.0)
        rm.record_pnl(20.0)
        rm.record_pnl(-5.0)
        assert sm.get_daily_pnl() == pytest.approx(25.0)

    def test_partial_fill_stake_tracked(self):
        """record_fill() should be callable without errors for any fill amount."""
        import bot.risk_manager as rm

        # Should not raise regardless of input
        rm.record_fill(0.0)
        rm.record_fill(50.0)
        rm.record_fill(999.99)


# ── get_stats() tests ──────────────────────────────────────────────────────────

class TestGetStats:
    def test_stats_returns_dict(self):
        import bot.risk_manager as rm
        stats = rm.get_stats()
        assert isinstance(stats, dict)
        required_keys = [
            "halted", "can_trade", "daily_pnl_usd", "daily_pnl_pct",
            "daily_start_balance", "open_positions", "max_positions",
            "loss_limit_pct", "paper_mode",
        ]
        for key in required_keys:
            assert key in stats, f"Missing key: {key}"

    def test_stats_pnl_pct_calculation(self):
        """daily_pnl_pct should be daily_pnl / daily_start_balance."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -100.0

        stats = rm.get_stats()
        assert stats["daily_pnl_pct"] == pytest.approx(-0.10, abs=0.001)

    def test_stats_open_positions_count(self):
        """open_positions in stats should reflect actual position count."""
        import bot.risk_manager as rm
        import bot.state_manager as sm

        sm._state["open_positions"] = [{"ticker": "X"}, {"ticker": "Y"}]
        stats = rm.get_stats()
        assert stats["open_positions"] == 2
