"""Phase 2 E5: tests for randomesque qid selection with graceful fallback.

The helper lives in ``services.question_bank._randomesque_pick``. It
should:

1. Fall back to ``random.shuffle`` when ``services.rating_service``
   cannot be imported.
2. Fall back to ``random.shuffle`` when no candidate has a rating.
3. When ratings are present, uniformly draw the first element from the
   top-M candidates closest to the user's theta; the rest are shuffled
   behind.
4. With ``m=1`` the single closest item always wins (deterministic).
5. Return ``[]`` for empty input without raising.
"""
import builtins
import random
import sys
import types

import pytest

from services import question_bank
from services.question_bank import _randomesque_pick


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _install_fake_rating_service(ratings, theta):
    """Register a synthetic ``services.rating_service`` module for the
    duration of a test. Returns the module so callers can tweak it.
    """
    mod = types.ModuleType("services.rating_service")
    mod.get_user_theta = lambda: theta
    mod.get_rating = lambda qid: ratings.get(qid)
    sys.modules["services.rating_service"] = mod
    return mod


def _remove_fake_rating_service():
    sys.modules.pop("services.rating_service", None)


@pytest.fixture(autouse=True)
def _clean_rating_service_module():
    """Every test starts with no fake rating_service installed, and
    cleanup happens even on failure."""
    _remove_fake_rating_service()
    yield
    _remove_fake_rating_service()


# --------------------------------------------------------------------------
# Basic shape
# --------------------------------------------------------------------------

def test_empty_input_returns_empty_list():
    assert _randomesque_pick([]) == []
    assert _randomesque_pick([], m=10) == []


def test_input_not_mutated():
    original = [1, 2, 3, 4, 5]
    before = list(original)
    _randomesque_pick(original)
    assert original == before


def test_tuple_input_accepted():
    # Callers sometimes pass a tuple / generator expression.
    result = _randomesque_pick((1, 2, 3))
    assert sorted(result) == [1, 2, 3]


# --------------------------------------------------------------------------
# Graceful degradation — no rating_service module
# --------------------------------------------------------------------------

def test_falls_back_to_shuffle_when_rating_service_missing(monkeypatch):
    """With no ``services.rating_service`` importable, the helper must
    return a permutation of the input — exactly as ``random.shuffle``
    would — rather than raise.
    """
    # Force ImportError for services.rating_service even if another
    # worktree/sibling has it installed.
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "services.rating_service" or name.endswith(
                "rating_service"):
            raise ImportError("rating_service not available in this worktree")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    ids = list(range(20))
    result = _randomesque_pick(ids)
    assert sorted(result) == ids
    assert len(result) == len(ids)


def test_falls_back_when_no_candidate_has_a_rating():
    """rating_service is installed, but every ``get_rating`` returns
    None → behaves like a plain shuffle."""
    _install_fake_rating_service(ratings={}, theta=0.0)
    ids = list(range(10))
    result = _randomesque_pick(ids)
    assert sorted(result) == ids


def test_falls_back_when_rating_service_raises():
    """A broken rating_service must never crash the selector — just
    fall through to ``random.shuffle``."""
    mod = types.ModuleType("services.rating_service")

    def _boom(*_a, **_k):
        raise RuntimeError("rating_service exploded")

    mod.get_user_theta = _boom
    mod.get_rating = _boom
    sys.modules["services.rating_service"] = mod

    ids = [1, 2, 3, 4, 5]
    result = _randomesque_pick(ids)
    assert sorted(result) == sorted(ids)


# --------------------------------------------------------------------------
# Theta-aware ranking
# --------------------------------------------------------------------------

def test_m_equals_one_is_deterministic_closest_wins():
    """With ``m=1`` the single closest item to theta is the only
    candidate for the front slot, so it always wins regardless of the
    RNG state."""
    # qid 3 sits at rating 0.05 — closest to theta=0.
    ratings = {1: -1.0, 2: -0.5, 3: 0.05, 4: 0.8, 5: 1.2}
    _install_fake_rating_service(ratings, theta=0.0)

    for seed in range(50):
        random.seed(seed)
        result = _randomesque_pick([1, 2, 3, 4, 5], m=1)
        assert result[0] == 3, f"expected qid 3 first (seed={seed})"
        assert sorted(result) == [1, 2, 3, 4, 5]


def test_top_m_front_bias_at_theta_zero():
    """With theta=0 and a spread of ratings, the first element should
    land within the top-M closest over a few hundred trials — never in
    the tail."""
    ratings = {
        10: -3.0, 11: -2.5, 12: -1.0,  # far-left tail
        20: -0.1, 21: 0.0, 22: 0.15, 23: 0.3, 24: 0.4,  # near theta
        30: 1.5, 31: 2.3, 32: 3.1,     # far-right tail
    }
    _install_fake_rating_service(ratings, theta=0.0)

    near_set = {20, 21, 22, 23, 24}  # top-5 by |rating|
    ids = list(ratings.keys())

    firsts = []
    random.seed(4242)
    for _ in range(400):
        result = _randomesque_pick(list(ids), m=5)
        firsts.append(result[0])

    # Every draw's first element must be one of the top-5 closest.
    assert set(firsts).issubset(near_set), (
        f"first-slot escaped top-M: {set(firsts) - near_set}")
    # And the distribution should actually exercise multiple of them
    # (otherwise the "uniform among top-M" claim is untested).
    assert len(set(firsts)) >= 3


def test_partial_ratings_use_only_rated_candidates_for_front_slot():
    """If only some candidates have ratings, the helper should still
    bias the front toward the closest RATED item — unrated qids are
    part of the tail shuffle, not the front pool."""
    ratings = {100: 0.05, 200: 2.0}  # 300, 400 unrated
    _install_fake_rating_service(ratings, theta=0.0)

    firsts = []
    random.seed(7)
    for _ in range(80):
        result = _randomesque_pick([100, 200, 300, 400], m=1)
        firsts.append(result[0])
    # m=1 + only one rating closest to theta → deterministic winner.
    assert set(firsts) == {100}


# --------------------------------------------------------------------------
# Feature flag
# --------------------------------------------------------------------------

def test_flag_disabled_bypasses_rating_service(monkeypatch):
    """When ``RANDOMESQUE_ENABLED`` is False, even a fully-working
    rating_service must be ignored and the helper should behave like
    plain ``random.shuffle``."""
    # Ratings strongly favour qid 1 — if randomesque were active, qid 1
    # would win the front slot on m=1.
    _install_fake_rating_service({1: 0.0, 2: 5.0, 3: -5.0}, theta=0.0)
    monkeypatch.setattr(question_bank, "RANDOMESQUE_ENABLED", False)

    ids = [1, 2, 3]
    firsts = set()
    random.seed(0)
    for _ in range(60):
        firsts.add(_randomesque_pick(list(ids), m=1)[0])
    # With pure random, all three should appear in the front slot
    # across 60 trials (probability of missing any one is ~(2/3)^60 ≈
    # 2.6e-11).
    assert firsts == {1, 2, 3}


# --------------------------------------------------------------------------
# Clamping
# --------------------------------------------------------------------------

def test_m_larger_than_pool_is_clamped():
    ratings = {1: 0.1, 2: 0.2}
    _install_fake_rating_service(ratings, theta=0.0)
    # m=100 but only 2 candidates — should not raise and should still
    # return both.
    result = _randomesque_pick([1, 2], m=100)
    assert sorted(result) == [1, 2]


def test_m_zero_or_negative_is_clamped_to_one():
    ratings = {1: 5.0, 2: 0.0, 3: -5.0}  # qid 2 closest to theta=0
    _install_fake_rating_service(ratings, theta=0.0)
    random.seed(1)
    for m in (0, -3):
        result = _randomesque_pick([1, 2, 3], m=m)
        assert result[0] == 2
        assert sorted(result) == [1, 2, 3]
