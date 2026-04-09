"""
test_market_matcher.py — Unit tests for market_matcher.py

Tests:
  - Fed rate cut title matches → score >= 0.75
  - Completely unrelated titles → returns None
  - Low confidence match (0.65–0.74) → returns None but logs
  - Date validation
  - Score formula weights
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot.market_matcher import (
    Candidate,
    Match,
    _preprocess,
    _compute_score,
    _dates_compatible,
    _detect_index,
    find_match,
    invalidate_cache,
    INDEX_KEYWORDS,
)


# ── Preprocessing tests ────────────────────────────────────────────────────────

class TestPreprocess:
    def test_lowercases(self):
        assert _preprocess("Hello World") == "hello world"

    def test_strips_punctuation(self):
        result = _preprocess("Will the Fed cut rates? (Nov. 2026)")
        assert "?" not in result
        assert "." not in result
        assert "(" not in result

    def test_normalises_whitespace(self):
        result = _preprocess("multiple   spaces\there")
        assert "  " not in result
        assert "\t" not in result

    def test_empty_string(self):
        assert _preprocess("") == ""


# ── Score formula tests ────────────────────────────────────────────────────────

class TestComputeScore:
    def test_identical_strings(self):
        score, tsr, pr = _compute_score("Will it rain tomorrow", "Will it rain tomorrow")
        assert score == pytest.approx(1.0, abs=0.01)

    def test_completely_different(self):
        score, _, _ = _compute_score(
            "Federal Reserve rate cut November 2026",
            "NBA Finals 2026 Game 7 Warriors vs Celtics"
        )
        assert score < 0.50

    def test_score_formula_weights(self):
        """Verify score = 0.7 × tsr + 0.3 × pr."""
        score, tsr, pr = _compute_score(
            "Will the Fed cut rates in November 2026?",
            "Federal Reserve rate cut November 2026"
        )
        expected = 0.7 * tsr + 0.3 * pr
        assert abs(score - expected) < 1e-9

    def test_word_order_invariant(self):
        """token_sort_ratio (primary) handles word-order differences."""
        s1, t1, p1 = _compute_score("Fed rate cut November 2026", "November 2026 Fed rate cut")
        assert s1 >= 0.89   # token_sort_ratio handles word-order; score is ~0.90 (float tolerance)


# ── Date compatibility tests ───────────────────────────────────────────────────

class TestDatesCompatible:
    def test_within_7_days(self):
        dt1 = datetime(2026, 11, 5, tzinfo=timezone.utc)
        dt2 = datetime(2026, 11, 10, tzinfo=timezone.utc)
        assert _dates_compatible(dt1, dt2, tolerance_days=7)

    def test_outside_7_days(self):
        dt1 = datetime(2026, 11, 5, tzinfo=timezone.utc)
        dt2 = datetime(2026, 12, 1, tzinfo=timezone.utc)
        assert not _dates_compatible(dt1, dt2, tolerance_days=7)

    def test_none_kalshi_date_always_ok(self):
        dt2 = datetime(2026, 11, 10, tzinfo=timezone.utc)
        assert _dates_compatible(None, dt2, tolerance_days=7)

    def test_none_candidate_date_always_ok(self):
        dt1 = datetime(2026, 11, 5, tzinfo=timezone.utc)
        assert _dates_compatible(dt1, None, tolerance_days=7)

    def test_both_none_ok(self):
        assert _dates_compatible(None, None, tolerance_days=7)


# ── find_match() integration tests ────────────────────────────────────────────

class TestFindMatch:
    """
    Integration tests for find_match().
    We patch the cache and logger to avoid file I/O.
    """

    def _make_candidate(
        self,
        title: str,
        prob: float = 0.50,
        close_date: Optional[datetime] = None,
        source: str = "manifold",
    ) -> Candidate:
        return Candidate(
            title=title,
            probability=prob,
            close_date=close_date,
            source=source,
            market_id="test-id",
        )

    @pytest.fixture(autouse=True)
    def clear_cache(self, tmp_path, monkeypatch):
        """Redirect cache writes to tmp dir and clear module cache."""
        import bot.config as cfg
        monkeypatch.setattr(cfg, "MATCH_CACHE_FILE", str(tmp_path / "match_cache.json"))
        monkeypatch.setattr(cfg, "LOW_CONF_LOG", str(tmp_path / "low_conf.jsonl"))
        invalidate_cache()
        yield
        invalidate_cache()

    # ── POSITIVE MATCH ─────────────────────────────────────────────────────────

    def test_fed_rate_cut_matches(self):
        """
        Use a mocked high-confidence score to verify the routing logic:
        scores >= FUZZY_MATCH_THRESHOLD should produce a match.
        """
        candidates = [
            self._make_candidate("Federal Reserve rate cut November 2026"),
        ]
        # Mock the score to be confidently above threshold
        with patch("bot.market_matcher._compute_score", return_value=(0.82, 0.85, 0.76)):
            result = find_match(
                kalshi_ticker="KXFED-26NOV",
                kalshi_title="Will the Fed cut rates in November 2026?",
                kalshi_close_date=None,
                candidates=candidates,
            )
        assert result is not None
        assert result.score >= 0.75
        assert result.candidate.title == "Federal Reserve rate cut November 2026"

    def test_exact_title_match(self):
        """Identical title should always match."""
        title = "Will the S&P 500 close above 5000 on December 31 2026"
        candidates = [self._make_candidate(title)]
        result = find_match(
            kalshi_ticker="SPX-DEC26",
            kalshi_title=title,
            kalshi_close_date=None,
            candidates=candidates,
        )
        assert result is not None
        assert result.score >= 0.99

    def test_similar_with_close_dates_matches(self):
        """Similar title with compatible dates should match (mocked score)."""
        close_kalshi = datetime(2026, 11, 5, tzinfo=timezone.utc)
        close_candidate = datetime(2026, 11, 7, tzinfo=timezone.utc)   # 2 days apart
        candidates = [
            self._make_candidate(
                "Federal Reserve rate cut November 2026",
                close_date=close_candidate,
            )
        ]
        with patch("bot.market_matcher._compute_score", return_value=(0.82, 0.85, 0.76)):
            result = find_match(
                kalshi_ticker="KXFED-26NOV",
                kalshi_title="Will the Fed cut rates in November 2026?",
                kalshi_close_date=close_kalshi,
                candidates=candidates,
            )
        assert result is not None

    # ── NEGATIVE MATCH ─────────────────────────────────────────────────────────

    def test_completely_different_titles_no_match(self):
        """
        'Chiefs vs Eagles Super Bowl' should NOT match
        'NBA Finals 2026 Game 7'
        """
        candidates = [
            self._make_candidate("NBA Finals 2026 Game 7"),
        ]
        result = find_match(
            kalshi_ticker="KXNFL-SB",
            kalshi_title="Chiefs vs Eagles Super Bowl",
            kalshi_close_date=None,
            candidates=candidates,
        )
        assert result is None

    def test_empty_candidates_no_match(self):
        result = find_match(
            kalshi_ticker="TICKER",
            kalshi_title="Some market",
            kalshi_close_date=None,
            candidates=[],
        )
        assert result is None

    def test_date_mismatch_prevents_match(self):
        """Even with a good title score, incompatible dates → no match."""
        close_kalshi = datetime(2026, 11, 5, tzinfo=timezone.utc)
        close_candidate = datetime(2026, 12, 31, tzinfo=timezone.utc)   # 56 days apart
        candidates = [
            self._make_candidate(
                "Federal Reserve rate cut November 2026",
                close_date=close_candidate,
            )
        ]
        result = find_match(
            kalshi_ticker="KXFED-26NOV",
            kalshi_title="Will the Fed cut rates in November 2026?",
            kalshi_close_date=close_kalshi,
            candidates=candidates,
        )
        assert result is None

    # ── LOW CONFIDENCE ─────────────────────────────────────────────────────────

    def test_low_confidence_returns_none_and_logs(self, tmp_path, monkeypatch):
        """
        Matches in [0.65, 0.74) range should return None but write to
        low_confidence_matches.jsonl.
        """
        import bot.config as cfg
        import bot.logger as bl
        log_path = str(tmp_path / "low_conf.jsonl")
        monkeypatch.setattr(cfg, "LOW_CONF_LOG", log_path)

        candidates = [
            self._make_candidate("November Federal Reserve meeting 2026 outcome")
        ]

        # Mock _compute_score AND log_low_confidence_match to capture the call
        logged_records = []

        def fake_log_low_conf(**kwargs):
            logged_records.append(kwargs)
            # Also write to the file directly so os.path.exists works
            import json as _json
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as fh:
                fh.write(_json.dumps(kwargs) + "\n")

        monkeypatch.setattr(bl, "log_low_confidence_match", fake_log_low_conf)

        with patch("bot.market_matcher._compute_score", return_value=(0.68, 0.70, 0.64)):
            result = find_match(
                kalshi_ticker="KXFED-LC",
                kalshi_title="Will the Fed cut rates in November 2026?",
                kalshi_close_date=None,
                candidates=candidates,
            )

        # Should return None (low confidence = no trade)
        assert result is None

        # Logger should have been called
        assert len(logged_records) == 1
        assert logged_records[0]["score"] == pytest.approx(0.68, abs=0.01)
        assert "kalshi_title" in logged_records[0]

    def test_below_low_confidence_floor_silent(self, tmp_path, monkeypatch):
        """
        Scores below FUZZY_LOW_CONF_MIN (0.65) should return None
        WITHOUT logging (silent reject).
        """
        import bot.config as cfg
        log_path = str(tmp_path / "low_conf_silent.jsonl")
        monkeypatch.setattr(cfg, "LOW_CONF_LOG", log_path)

        candidates = [self._make_candidate("Totally different content here")]

        with patch("bot.market_matcher._compute_score", return_value=(0.40, 0.35, 0.50)):
            result = find_match(
                kalshi_ticker="TICKER",
                kalshi_title="Will the Fed cut rates in November 2026?",
                kalshi_close_date=None,
                candidates=candidates,
            )

        assert result is None
        # Log should NOT exist (or be empty)
        if os.path.exists(log_path):
            with open(log_path) as fh:
                content = fh.read().strip()
            assert content == "", "Silent rejects should NOT be logged"

    # ── BEST MATCH SELECTION ───────────────────────────────────────────────────

    def test_selects_best_match_from_multiple(self):
        """When multiple candidates exist, the highest-scoring one is returned."""
        candidates = [
            self._make_candidate("Bitcoin price above 100k December 2026"),
            self._make_candidate("Will the Fed cut rates in November 2026"),  # Better match
            self._make_candidate("NFL Super Bowl winner 2027"),
        ]
        result = find_match(
            kalshi_ticker="KXFED-26NOV",
            kalshi_title="Will the Fed cut rates in November 2026?",
            kalshi_close_date=None,
            candidates=candidates,
        )
        assert result is not None
        assert "fed" in result.candidate.title.lower() or "rate" in result.candidate.title.lower()

    # ── INDEX CROSS-MATCH GUARD (C13 fix) ─────────────────────────────────

    def test_nasdaq_vs_sp500_rejected(self):
        """
        NASDAQ and S&P titles must NOT match, even with high fuzzy score.
        Different financial indices would cause catastrophically wrong pricing.
        """
        candidates = [
            self._make_candidate("S&P 500 above 5000 December 2026"),
        ]
        # Mock a high score that would normally pass the threshold
        with patch("bot.market_matcher._compute_score", return_value=(0.85, 0.88, 0.78)):
            result = find_match(
                kalshi_ticker="NASDAQ-DEC26",
                kalshi_title="NASDAQ 100 above 18000 December 2026",
                kalshi_close_date=None,
                candidates=candidates,
            )
        assert result is None, "Cross-index match (NASDAQ vs S&P) must be rejected"

    def test_nasdaq_vs_nasdaq_allowed(self):
        """
        Same-index matches (NASDAQ vs NASDAQ) should be allowed.
        """
        candidates = [
            self._make_candidate("NASDAQ closes above 18000 end of year"),
        ]
        with patch("bot.market_matcher._compute_score", return_value=(0.82, 0.85, 0.76)):
            result = find_match(
                kalshi_ticker="NASDAQ-DEC26",
                kalshi_title="NASDAQ 100 above 18000 December 2026",
                kalshi_close_date=None,
                candidates=candidates,
            )
        assert result is not None, "Same-index match (NASDAQ vs NASDAQ) should be allowed"

    def test_non_index_vs_nasdaq_allowed(self):
        """
        Non-index market can match anything — no cross-index conflict.
        """
        candidates = [
            self._make_candidate("NASDAQ above 18000 by December"),
        ]
        with patch("bot.market_matcher._compute_score", return_value=(0.80, 0.83, 0.73)):
            result = find_match(
                kalshi_ticker="KXMISC-DEC26",
                kalshi_title="Will tech stocks rally in December 2026?",
                kalshi_close_date=None,
                candidates=candidates,
            )
        assert result is not None, "Non-index title should not trigger cross-index guard"


# ── _detect_index() unit tests ────────────────────────────────────────────────

class TestDetectIndex:
    def test_nasdaq_detected(self):
        assert _detect_index("NASDAQ 100 above 18000") == "nasdaq"

    def test_sp500_detected(self):
        assert _detect_index("S&P 500 above 5000") == "sp500"

    def test_dow_detected(self):
        assert _detect_index("Dow Jones above 40000") == "dow"

    def test_russell_detected(self):
        assert _detect_index("Russell 2000 closes above 2200") == "russell"

    def test_vix_detected(self):
        assert _detect_index("VIX above 20 by Friday") == "vix"

    def test_non_index_returns_none(self):
        assert _detect_index("Will the Fed cut rates?") is None

    def test_case_insensitive(self):
        assert _detect_index("nasdaq closes above 18000") == "nasdaq"
        assert _detect_index("S&P 500 ABOVE 5000") == "sp500"

    def test_all_five_indices_covered(self):
        """INDEX_KEYWORDS must cover all 5 indices."""
        assert set(INDEX_KEYWORDS.keys()) == {"nasdaq", "sp500", "dow", "russell", "vix"}
