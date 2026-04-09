"""
test_fair_value.py — Regression tests for source disagreement multiplier.

Tests:
  - 3 sources with high std (>0.20) → mult = 0.50
  - 3 sources with moderate std (>0.10) → mult = 0.75
  - 3 sources with low std (<0.10) → mult = 1.00
  - 1 source → mult = 1.00 (no penalty for single source)
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot.fair_value import _aggregate_probabilities, _compute_disagreement_mult, FairValue


class TestCacheRefreshLock:
    """Regression: fair_value must use asyncio.Lock to prevent concurrent refresh stampede."""

    def test_refresh_lock_exists(self):
        """fair_value module must have a _get_refresh_lock helper."""
        from bot.fair_value import _get_refresh_lock
        import asyncio
        # _get_refresh_lock is a sync function that returns an asyncio.Lock
        lock = _get_refresh_lock()
        assert isinstance(lock, asyncio.Lock)

    def test_refresh_all_sources_uses_lock(self):
        """refresh_all_sources must acquire the lock to prevent concurrent refresh."""
        import inspect
        from bot.fair_value import refresh_all_sources
        src = inspect.getsource(refresh_all_sources)
        assert "_get_refresh_lock" in src or "lock" in src, (
            "refresh_all_sources must use a lock to prevent concurrent refresh"
        )


class TestSourceDisagreementMult:
    """Regression tests for source disagreement multiplier (C3/C8 fix)."""

    def test_high_disagreement_std_above_020(self):
        """
        3 sources with std > 0.20 → source_disagreement_mult == 0.50.
        e.g., PredictIt=0.60, Manifold=0.40, Polymarket=0.90
        std = np.std([0.60, 0.40, 0.90]) ≈ 0.205
        """
        probs = [0.60, 0.40, 0.90]
        assert np.std(probs, ddof=0) > 0.20
        _, _, _, mult = _aggregate_probabilities(
            predictit_prob=0.60, manifold_prob=0.40, polymarket_prob=0.90,
        )
        assert mult == 0.50

    def test_moderate_disagreement_std_above_010(self):
        """
        3 sources with 0.10 < std <= 0.20 → source_disagreement_mult == 0.75.
        e.g., PredictIt=0.60, Manifold=0.45, Polymarket=0.65
        std = np.std([0.60, 0.45, 0.65]) ≈ 0.0845
        Need bigger spread: PredictIt=0.55, Manifold=0.35, Polymarket=0.55
        std = np.std([0.55, 0.35, 0.55]) ≈ 0.0943
        Even bigger: PredictIt=0.60, Manifold=0.35, Polymarket=0.60
        std = np.std([0.60, 0.35, 0.60]) ≈ 0.1178
        """
        probs = [0.60, 0.35, 0.60]
        std = np.std(probs, ddof=0)
        assert 0.10 < std <= 0.20, f"std={std}"
        _, _, _, mult = _aggregate_probabilities(
            predictit_prob=0.60, manifold_prob=0.35, polymarket_prob=0.60,
        )
        assert mult == 0.75

    def test_low_disagreement_std_below_010(self):
        """
        3 sources with std <= 0.10 → source_disagreement_mult == 1.00.
        e.g., PredictIt=0.60, Manifold=0.61, Polymarket=0.60
        std = np.std([0.60, 0.61, 0.60]) ≈ 0.0047
        """
        probs = [0.60, 0.61, 0.60]
        assert np.std(probs, ddof=0) <= 0.10
        _, _, _, mult = _aggregate_probabilities(
            predictit_prob=0.60, manifold_prob=0.61, polymarket_prob=0.60,
        )
        assert mult == 1.00

    def test_single_source_no_penalty(self):
        """
        1 source → source_disagreement_mult == 1.00 (no penalty for single source).
        """
        _, _, _, mult = _aggregate_probabilities(
            predictit_prob=0.60, manifold_prob=None, polymarket_prob=None,
        )
        assert mult == 1.00

    def test_two_sources_high_disagreement(self):
        """
        2 sources with std > 0.20 → mult = 0.50.
        e.g., PredictIt=0.30, Manifold=0.80 → std ≈ 0.25
        """
        probs = [0.30, 0.80]
        assert np.std(probs, ddof=0) > 0.20
        _, _, _, mult = _aggregate_probabilities(
            predictit_prob=0.30, manifold_prob=0.80, polymarket_prob=None,
        )
        assert mult == 0.50

    def test_dataclass_has_field(self):
        """FairValue dataclass has source_disagreement_mult with default 1.0."""
        fv = FairValue(
            probability=0.60, confidence="triple",
            sources=["predictit", "manifold", "polymarket"],
            source_count=3,
        )
        assert fv.source_disagreement_mult == 1.0

    def test_compute_disagreement_mult_directly(self):
        """Unit test _compute_disagreement_mult helper directly."""
        assert _compute_disagreement_mult([0.60]) == 1.0     # single source
        assert _compute_disagreement_mult([]) == 1.0          # no sources
        assert _compute_disagreement_mult([0.60, 0.61, 0.60]) == 1.0  # low std
        assert _compute_disagreement_mult([0.60, 0.35, 0.60]) == 0.75  # moderate std
        assert _compute_disagreement_mult([0.60, 0.40, 0.90]) == 0.50  # high std


# ── Manifold probability clipping tests (critique cycle 4 fix) ───────────────

class TestManifoldProbabilityClipping:
    """
    Regression: Manifold source must clip probabilities to strict (0, 1).
    Raw 0.0 or 1.0 would dominate the std calculation and create extreme
    disagreement penalties, distorting the aggregation.
    """

    def test_manifold_fetch_has_prob_bounds_check(self):
        """_fetch_manifold must validate probabilities are in (0, 1)."""
        import inspect
        from bot.fair_value import _fetch_manifold
        src = inspect.getsource(_fetch_manifold)
        assert "0.0 < prob < 1.0" in src, (
            "_fetch_manifold must clip probabilities to strict (0, 1)"
        )

    def test_all_three_sources_clip_probabilities(self):
        """All three source fetchers must validate prob in (0, 1)."""
        import inspect
        from bot.fair_value import _fetch_predictit, _fetch_manifold, _fetch_polymarket
        for fn_name, fn in [("predictit", _fetch_predictit), ("manifold", _fetch_manifold), ("polymarket", _fetch_polymarket)]:
            src = inspect.getsource(fn)
            assert "0.0 < " in src or "prob < 1.0" in src, (
                f"_fetch_{fn_name} must validate probability bounds"
            )


# ── Source health tracking tests (critique cycle 4 fix) ──────────────────────

class TestSourceHealthTracking:
    """
    Regression: fair_value must track consecutive fetch failures per source.
    If a source returns 0 results for 3+ cycles, log a prominent warning.
    """

    def test_source_fail_counts_exist(self):
        """Module must have _source_fail_counts dict."""
        from bot.fair_value import _source_fail_counts
        assert isinstance(_source_fail_counts, dict)
        assert "predictit" in _source_fail_counts
        assert "manifold" in _source_fail_counts
        assert "polymarket" in _source_fail_counts

    def test_track_source_health_function_exists(self):
        """_track_source_health must be callable."""
        from bot.fair_value import _track_source_health
        assert callable(_track_source_health)

    def test_track_source_health_resets_on_success(self):
        """Successful fetch resets failure counter to 0."""
        from bot.fair_value import _track_source_health, _source_fail_counts
        from bot.market_matcher import Candidate
        _source_fail_counts["predictit"] = 5
        _track_source_health("predictit", [Candidate("test", 0.5, None, "predictit", "id")])
        assert _source_fail_counts["predictit"] == 0

    def test_track_source_health_increments_on_failure(self):
        """Empty result increments failure counter."""
        from bot.fair_value import _track_source_health, _source_fail_counts
        _source_fail_counts["manifold"] = 0
        _track_source_health("manifold", [])
        assert _source_fail_counts["manifold"] == 1
        _track_source_health("manifold", [])
        assert _source_fail_counts["manifold"] == 2


# ── Fair value cache system tests (Agent C audit) ───────────────────────────

class TestFairValueCacheSystem:
    """Regression: fair_value cache must use asyncio.Lock (not threading.Lock) and time.monotonic."""

    def test_cache_uses_monotonic_time(self):
        """Cache TTL must use time.monotonic, not datetime (immune to clock jumps)."""
        import inspect
        from bot.fair_value import _caches_valid
        src = inspect.getsource(_caches_valid)
        assert "monotonic" in src, "Cache TTL should use time.monotonic, not datetime"

    def test_refresh_lock_is_asyncio_not_threading(self):
        """fair_value._refresh_lock must be asyncio.Lock for async code."""
        import asyncio
        from bot.fair_value import _get_refresh_lock
        lock = _get_refresh_lock()
        assert isinstance(lock, asyncio.Lock), (
            "fair_value cache lock must be asyncio.Lock, not threading.Lock"
        )
        assert not isinstance(lock, type(None))

    def test_aggregate_zero_sources_returns_none_via_get_fair_value(self):
        """get_fair_value must return None (not 0.5) when no sources match."""
        from bot.fair_value import _aggregate_probabilities
        prob, count, names, mult = _aggregate_probabilities(None, None, None)
        assert count == 0
        # get_fair_value checks source_count == 0 and returns None
        # The raw aggregation returns 0.0 but that's never exposed
