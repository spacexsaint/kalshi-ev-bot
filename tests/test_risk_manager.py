"""
test_risk_manager.py — Unit tests for upgraded risk_manager.py

Tests:
  - can_trade() now returns (bool, reason) tuple
  - Correlation-aware position limiting
  - Category detection (including NASDAQ)
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
    rm._pnl_warning_sent = False

    import bot.state_manager as sm
    sm._state = sm._default_state()
    sm._state["daily_start_balance"] = 1000.0
    sm._state["daily_pnl"] = 0.0

    yield

    rm._halted = False
    rm._pnl_warning_sent = False


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


# ── Early P&L warning tests (critique cycle 4 fix) ───────────────────────────

class TestEarlyPnlWarning:
    """
    Regression: risk_manager must log a warning when daily P&L reaches
    67% of the circuit breaker threshold, before full halt.
    """

    def test_warning_flag_exists(self):
        """risk_manager must track _pnl_warning_sent."""
        import bot.risk_manager as rm
        assert hasattr(rm, "_pnl_warning_sent")

    def test_warning_sent_before_halt(self):
        """
        At -10% loss (67% of 15% limit), warning should be sent.
        Trading should still be allowed (not halted).
        """
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -101.0  # -10.1% > 67% of 15%=10.05%
        rm._halted = False
        rm._pnl_warning_sent = False
        allowed, reason = rm.can_trade()
        assert allowed is True, "Trading should still be allowed at -10%"
        assert rm._pnl_warning_sent is True, "Warning flag should be set"

    def test_warning_not_sent_for_small_loss(self):
        """Small losses should NOT trigger the early warning."""
        import bot.risk_manager as rm
        import bot.state_manager as sm
        sm._state["daily_start_balance"] = 1000.0
        sm._state["daily_pnl"] = -50.0  # -5% < 67% of 15%=10%
        rm._halted = False
        rm._pnl_warning_sent = False
        rm.can_trade()
        assert rm._pnl_warning_sent is False

    def test_warning_reset_on_daily_reset(self):
        """Daily reset must clear the warning flag."""
        import bot.risk_manager as rm
        rm._pnl_warning_sent = True
        rm.reset_daily(1000.0)
        assert rm._pnl_warning_sent is False


# ── NASDAQ correlation detection (Agent B fix) ───────────────────────────────

class TestNasdaqCorrelation:
    """
    Regression: NASDAQ index markets like "NASDAQ >19000" and "NASDAQ >19500"
    must be detected as the same category so correlation limits apply.
    Without this, the bot could open unlimited correlated NASDAQ positions.
    """

    def test_nasdaq_detected_as_category(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("Will NASDAQ close above 19000?") == "nasdaq"

    def test_nasdaq100_detected(self):
        from bot.risk_manager import get_position_category
        assert get_position_category("NASDAQ100 above 20000 today") == "nasdaq"

    def test_two_nasdaq_markets_correlated(self):
        """Two NASDAQ markets at different strikes must share the same category."""
        from bot.risk_manager import get_position_category
        cat1 = get_position_category("NASDAQ >19000")
        cat2 = get_position_category("NASDAQ >19500")
        assert cat1 == cat2
        assert cat1 == "nasdaq"

    def test_nasdaq_blocked_at_max(self):
        """NASDAQ positions at MAX_POSITIONS_PER_CATEGORY must be blocked."""
        from bot.risk_manager import get_correlation_stake_multiplier
        import bot.state_manager as sm
        sm._state["open_positions"] = [
            {
                "ticker": f"NASDAQ100-{i}",
                "market_title": f"NASDAQ above {19000 + i * 500}",
                "category": "nasdaq",
                "direction": "YES",
                "entry_price_cents": 50,
                "contracts": 10.0,
                "stake_usd": 50.0,
                "fair_prob_at_entry": 0.60,
                "net_edge_at_entry": 0.08,
                "opened_at": "2026-04-08T12:00:00+00:00",
                "client_order_id": f"uuid-nasdaq-{i}",
            }
            for i in range(config.MAX_POSITIONS_PER_CATEGORY)
        ]
        mult, cat = get_correlation_stake_multiplier("NASDAQ above 20500")
        assert mult == 0.0
        assert cat == "nasdaq"


# ── Emergency 5xx pause (Agent B fix) ────────────────────────────────────────

class TestEmergency5xxPause:
    """
    Regression: kalshi_client must track consecutive 5xx errors and
    expose the counter for risk management.
    """

    def test_consecutive_5xx_counter_exists(self):
        from bot.kalshi_client import get_consecutive_5xx, reset_consecutive_5xx
        reset_consecutive_5xx()
        assert get_consecutive_5xx() == 0

    def test_reset_clears_counter(self):
        import bot.kalshi_client as kc
        kc._consecutive_5xx = 5
        kc.reset_consecutive_5xx()
        assert kc._consecutive_5xx == 0
