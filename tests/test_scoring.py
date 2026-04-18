"""
Regression tests for `services/scoring.py` covering the bugs the audit
turned up plus the basic happy-path for every subtype.

The tests are pure-Python (no DB needed) — they call ScoringEngine
directly with hand-built question_data dicts that mirror what
QuestionBankService.get_question() returns at runtime.
"""
import math

import pytest

from services.scoring import ScoringEngine


# ── Single-select (mcq_single, qc, rc_single, data_interp) ──

def test_single_select_correct():
    q = {
        "subtype": "mcq_single",
        "options": [
            {"label": "A", "text": "1", "is_correct": False},
            {"label": "B", "text": "2", "is_correct": True},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["B"]}) is True


def test_single_select_wrong():
    q = {
        "subtype": "qc",
        "options": [
            {"label": "A", "text": "x", "is_correct": True},
            {"label": "B", "text": "y", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["B"]}) is False


def test_single_select_empty_answer():
    q = {
        "subtype": "rc_single",
        "options": [{"label": "A", "text": "1", "is_correct": True}],
    }
    assert ScoringEngine.check_answer(q, {"selected": []}) is False


def test_single_select_too_many():
    """Marking 2 in a single-select question = wrong."""
    q = {
        "subtype": "mcq_single",
        "options": [
            {"label": "A", "text": "x", "is_correct": True},
            {"label": "B", "text": "y", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["A", "B"]}) is False


# ── Multi-select (mcq_multi, rc_multi) ──

def test_multi_all_or_nothing():
    q = {
        "subtype": "mcq_multi",
        "options": [
            {"label": "A", "text": "1", "is_correct": True},
            {"label": "B", "text": "2", "is_correct": True},
            {"label": "C", "text": "3", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["A", "B"]}) is True
    # Partial credit not allowed
    assert ScoringEngine.check_answer(q, {"selected": ["A"]}) is False
    # Extra selections fail
    assert ScoringEngine.check_answer(q, {"selected": ["A", "B", "C"]}) is False


# ── SE: exactly 2 correct ──

def test_se_correct():
    q = {
        "subtype": "se",
        "options": [
            {"label": "A", "text": "ascend", "is_correct": True},
            {"label": "B", "text": "rise", "is_correct": True},
            {"label": "C", "text": "fall", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["A", "B"]}) is True
    assert ScoringEngine.check_answer(q, {"selected": ["A"]}) is False


def test_se_wrong_count_correct_options():
    """Data corruption: SE row with only 1 marked correct -> always False, with warning."""
    q = {
        "subtype": "se",
        "options": [
            {"label": "A", "text": "x", "is_correct": True},
            {"label": "B", "text": "y", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": ["A"]}) is False


# ── TC: dictionary of blank -> choice ──

def test_tc_correct():
    q = {
        "subtype": "tc",
        "options": [
            {"label": "blank1_A", "text": "ubiquitous", "is_correct": True},
            {"label": "blank1_B", "text": "rare", "is_correct": False},
            {"label": "blank2_A", "text": "trivial", "is_correct": False},
            {"label": "blank2_B", "text": "profound", "is_correct": True},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": {"blank1": "A", "blank2": "B"}}) is True


def test_tc_partial_blanks_fail():
    q = {
        "subtype": "tc",
        "options": [
            {"label": "blank1_A", "text": "x", "is_correct": True},
            {"label": "blank2_A", "text": "y", "is_correct": True},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": {"blank1": "A"}}) is False


def test_tc_no_correct_options_returns_false():
    """REGRESSION: previously credited any answer because all() over {} == True."""
    q = {
        "subtype": "tc",
        "options": [
            {"label": "blank1_A", "text": "x", "is_correct": False},
            {"label": "blank1_B", "text": "y", "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": {}}) is False
    assert ScoringEngine.check_answer(q, {"selected": {"blank1": "A"}}) is False


def test_tc_single_blank_format():
    """REGRESSION: ~93 TC questions in the shipped bank use single-blank
    labels (A / B / C with no blank1_ prefix). Previously the scorer's
    `len(parts) == 2` check skipped them all, so every single-blank TC was
    marked wrong even when the user selected the correct option (and Show
    Answer correctly rendered it).

    UI emits {"selected": {"blank1": "A"}} for these (defaults to blank1
    when there's no underscore in the label) — scoring must match.
    """
    q = {
        "subtype": "tc",
        "options": [
            {"label": "A", "text": "ubiquitous", "is_correct": False},
            {"label": "B", "text": "rare",       "is_correct": True},
            {"label": "C", "text": "trivial",    "is_correct": False},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected": {"blank1": "B"}}) is True
    assert ScoringEngine.check_answer(q, {"selected": {"blank1": "A"}}) is False
    assert ScoringEngine.check_answer(q, {"selected": {}}) is False


# ── Numeric entry ──

def test_numeric_decimal_exact_match():
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 2.5, "tolerance": 0},
    }
    assert ScoringEngine.check_answer(q, {"value": "2.5"}) is True
    assert ScoringEngine.check_answer(q, {"value": "2.50"}) is True


def test_numeric_fraction_equivalent():
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 2.5, "tolerance": 0},
    }
    # 5/2 == 2.5
    assert ScoringEngine.check_answer(q, {"numerator": 5, "denominator": 2}) is True


def test_numeric_tolerance_boundary():
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 1.0, "tolerance": 0.05},
    }
    assert ScoringEngine.check_answer(q, {"value": "1.05"}) is True
    assert ScoringEngine.check_answer(q, {"value": "1.06"}) is False


def test_numeric_tolerance_none_does_not_crash():
    """REGRESSION: tolerance=None used to crash with TypeError."""
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 0.5, "tolerance": None},
    }
    assert ScoringEngine.check_answer(q, {"value": "0.5"}) is True


def test_numeric_malformed_exact_value_does_not_crash():
    """REGRESSION: non-numeric exact_value used to crash with ValueError."""
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": "abc", "tolerance": 0},
    }
    assert ScoringEngine.check_answer(q, {"value": "5"}) is False


def test_numeric_zero_denominator():
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 1.0, "tolerance": 0},
    }
    # User supplies fraction with d=0 → no crash, returns False.
    assert ScoringEngine.check_answer(q, {"numerator": 5, "denominator": 0}) is False


def test_numeric_no_user_answer():
    q = {
        "subtype": "numeric_entry",
        "numeric_answer": {"exact_value": 1.0, "tolerance": 0},
    }
    assert ScoringEngine.check_answer(q, {}) is False


# ── select-in-passage ──

def test_select_in_passage_correct():
    q = {
        "subtype": "rc_select_passage",
        "options": [
            {"label": "1", "text": "...", "is_correct": False},
            {"label": "2", "text": "...", "is_correct": True},
        ],
    }
    assert ScoringEngine.check_answer(q, {"selected_sentence": 2}) is True
    assert ScoringEngine.check_answer(q, {"selected_sentence": "2"}) is True


# ── estimate_scaled_score robustness ──

def test_estimate_scaled_score_handles_nonint():
    assert ScoringEngine.estimate_scaled_score("not a number") == (130, 135)
    assert ScoringEngine.estimate_scaled_score(None) == (130, 135)
    assert ScoringEngine.estimate_scaled_score(-5) == ScoringEngine.estimate_scaled_score(0)
    assert ScoringEngine.estimate_scaled_score(999) == ScoringEngine.estimate_scaled_score(27)


def test_estimate_scaled_score_unknown_band_falls_back_medium():
    assert ScoringEngine.estimate_scaled_score(20, "wat") == ScoringEngine.estimate_scaled_score(20, "medium")


# ── unknown subtype / malformed input ──

def test_unknown_subtype_returns_false():
    assert ScoringEngine.check_answer({"subtype": "wat"}, {"selected": ["A"]}) is False


def test_non_dict_inputs_return_false():
    assert ScoringEngine.check_answer(None, None) is False
    assert ScoringEngine.check_answer("string", {}) is False
    assert ScoringEngine.check_answer({"subtype": "mcq_single"}, "string") is False
