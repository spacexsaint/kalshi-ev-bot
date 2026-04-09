"""
test_risk_manager.py — Unit tests for upgraded risk_manager.py

Tests:
  - can_trade() now returns (bool, reason) tuple
  - Correlation-aware position limiting
  - Category detection
  - Circuit breaker, position cap, daily reset
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import config


@pytest.fixture(autouse=True)
def reset_risk_manager(tmp_path, monkeypatch):
    import bot.config as cfg
    state_path = str(tmp_path / "state.json")
    monkeypatch.setattr(cfg, "STATE_FILE", state_path)
    monkeypatch.setattr(cfg, "EVENTS_LOG", str(tmp_path / "events.jsonl"))

    import bot.risk_manager as rm
    rm._halted = False

    import bot.state_manager as sm
    sm._state = sm._default_state()
    sm._state["daily_start_balance"] = 1000.0
    sm._state["daily_pnl"] = 0.0

    yield

    rm._halted = False


# ── Category detection tests ───────────────────────────────────────────────────

class TestCategoryDetection:
    def test_fed_detected(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("Will the Fed cut rates in November?") == "fed_rates"

    def test_election_detected(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("2026 midterm election results") == "election"

    def test_btc_detected(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("Will Bitcoin hit $100k?") == "btc"

    def test_uncategorized(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("Some completely unique market") == "uncategorized"


# ── Correlation-aware sizing tests ─────────────────────────────────────────────

class TestCorrelationAwareSizing:
    def test_no_penalty_first_position(self):
        from bot.risk_manager import get_correlation_stake_multiplier
        import bot.state_manager as sm
        sm._state["open_positions"] = []
        mult, cat = get_correlation_stake_multiplier("Will the Fed cut rates?")
        assert mult == pytest.approx(1.0)
        assert cat == "fed_rates"

    def test_half_penalty_one_existing(self):
        from bot.risk_manager import get_correlation_stake_multiplier
        import bot.state_manager as sm
        sm._state["open_positions"] = [{
            "ticker": "KXFED-NOV",
            "market_title": "Will the Fed cut rates in October?",
            "category": "fed_rates",
            "direction": "YES",
            "entry_price_cents": 50,
            "contracts": 10.0,
            "stake_usd": 50.0,
            "fair_prob_at_entry": 0.60,
            "net_edge_at_entry": 0.08,
            "opened_at": "2026-04-08T12:00:00+00:00",
            "client_order_id": "uuid-0",
        }]
        mult, cat = get_correlation_stake_multiplier("Will the Fed cut rates in November?")
        assert mult == pytest.approx(config.CORRELATED_BET_SIZE_PENALTY)

    def test_blocked_at_max_positions_per_category(self):
        from bot.risk_manager import get_correlation_stake_multiplier
        import bot.state_manager as sm
        sm._state["open_positions"] = [
            {
                "ticker": f"KXFED-{i}",
                "market_title": f"Fed rate cut market {i}",
                "category": "fed_rates",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 10.0,
                "stake_usd": 50.0,
                "fair_prob_at_entry": 0.60,
                "net_edge_at_entry": 0.08,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-{i}",
            }
            for i in range(config.MAX_POSITIONS_PER_CATEGORY)
        ]
        mult, cat = get_correlation_stake_multiplier("Will the Fed cut rates in December?")
        assert mult == 0.0
        assert cat == "fed_rates"

    def test_uncategorized_no_penalty(self):
        """Uncategorised markets never get correlation penalty."""
        from bot.risk_manager import get_correlation_stake_multiplier
        import bot.state_manager as sm
        # Fill with uncategorised positions
        sm._state["open_positions"] = [
            {
                "ticker": f"MISC-{i}",
                "market_title": "Some unique uncategorized market",
                "category": "uncategorized",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 5.0,
                "stake_usd": 25.0,
                "fair_prob_at_entry": 0.55,
                "net_edge_at_entry": 0.06,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-{i}",
            }
            for i in range(5)
        ]
        mult, cat = get_correlation_stake_multiplier("Some other unique uncategorized market")
        assert mult == pytest.approx(1.0)


# ── can_trade() tests (now returns tuple) ──────────────────────────────────────

class TestCanTrade:
    def test_can_trade_healthy(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 0.0
        sm._state["open_positions"] = []
        allowed, reason = rm.can_trade()
        assert allowed is True
        assert reason == "ok"

    def test_cannot_trade_loss_limit(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -151.0
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "loss" in reason

    def test_cannot_trade_at_threshold(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -150.0
        allowed, reason = rm.can_trade()
        assert allowed is False

    def test_can_trade_just_below_threshold(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -149.0
        rm._halted = False
        allowed, reason = rm.can_trade()
        assert allowed is True

    def test_cannot_trade_position_cap(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["open_positions"] = [
            {"ticker": f"M-{i}", "category": "uncategorized", "market_title": "test"}
            for i in range(config.MAX_OPEN_POSITIONS)
        ]
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "position_cap" in reason

    def test_cannot_trade_correlation_block(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["open_positions"] = [
            {
                "ticker": f"KXFED-{i}",
                "market_title": "Fed rate cut market",
                "category": "fed_rates",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 5.0,
                "stake_usd": 25.0,
                "fair_prob_at_entry": 0.60,
                "net_edge_at_entry": 0.07,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-{i}",
            }
            for i in range(config.MAX_POSITIONS_PER_CATEGORY)
        ]
        allowed, reason = rm.can_trade("Will the Fed cut rates in January?")
        assert allowed is False
        assert "correlation_block" in reason

    def test_halted_blocks_trading(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 100.0
        rm._halted = True
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "halted" in reason

    def test_circuit_breaker_sets_halted(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -200.0
        rm.can_trade()
        assert rm._halted is True
        allowed, _ = rm.can_trade()
        assert allowed is False


# ── reset_daily() tests ────────────────────────────────────────────────────────

class TestResetDaily:
    def test_reset_clears_pnl(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = -200.0
        rm._halted = True
        rm.reset_daily(800.0)
        assert sm.get_daily_pnl() == 0.0

    def test_reset_clears_halted(self):
        import bot.risk_manager as rm
        rm._halted = True
        rm.reset_daily(1000.0)
        assert rm._halted is False

    def test_reset_updates_start_balance(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        rm.reset_daily(750.0)
        assert sm.get_daily_start_balance() == pytest.approx(750.0)

    def test_can_trade_after_reset(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = -500.0
        rm._halted = True
        sm._state["open_positions"] = []
        rm.reset_daily(500.0)
        allowed, _ = rm.can_trade()
        assert allowed is True


# ── record_pnl() tests ─────────────────────────────────────────────────────────

class TestRecordPnL:
    def test_positive_pnl(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 0.0
        rm.record_pnl(50.0)
        assert sm.get_daily_pnl() == pytest.approx(50.0)

    def test_negative_pnl(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 100.0
        rm.record_pnl(-30.0)
        assert sm.get_daily_pnl() == pytest.approx(70.0)

    def test_cumulative(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_pnl"] = 0.0
        rm.record_pnl(10.0)
        rm.record_pnl(20.0)
        rm.record_pnl(-5.0)
        assert sm.get_daily_pnl() == pytest.approx(25.0)

    def test_record_fill_no_error(self):
        import bot.risk_manager as rm
        rm.record_fill(0.0)
        rm.record_fill(50.0)


# ── get_stats() tests ──────────────────────────────────────────────────────────

class TestGetStats:
    def test_stats_returns_dict(self):
        import bot.risk_manager as rm
        stats = rm.get_stats()
        assert isinstance(stats, dict)
        required = [
            "halted", "can_trade", "can_trade_reason", "daily_pnl_usd",
            "daily_pnl_pct", "daily_start_balance", "open_positions",
            "max_positions", "loss_limit_pct", "paper_mode",
        ]
        for key in required:
            assert key in stats, f"Missing key: {key}"

    def test_pnl_pct_calculation(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -100.0
        stats = rm.get_stats()
        assert stats["daily_pnl_pct"] == pytest.approx(-0.10, abs=0.001)

    def test_open_positions_count(self):
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["open_positions"] = [{"ticker": "X"}, {"ticker": "Y"}]
        stats = rm.get_stats()
        assert stats["open_positions"] == 2
