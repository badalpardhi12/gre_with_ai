"""
Tests for `services.explanation.validate_explanation`.

The runtime gate has to flag the same bug classes that PR 1 + PR 2
retired (LLM self-correction artifacts, "correct answer is X" claims
that disagree with is_correct, swapped explanations) without false-
positively rejecting valid explanations.
"""
from services.explanation import validate_explanation


def _q(options, **kw):
    return {"id": 1, "subtype": "mcq_single", "options": options, **kw}


def test_valid_explanation_passes():
    q = _q([
        {"label": "A", "text": "the chicanery of a trickster", "is_correct": True},
        {"label": "B", "text": "honesty",                      "is_correct": False},
    ])
    expl = "Loki is described as a trickster, so chicanery (A) fits the blank."
    ok, reason = validate_explanation(expl, q)
    assert ok is True
    assert reason == ""


def test_self_correction_artifact_rejected():
    q = _q([
        {"label": "A", "text": "x", "is_correct": False},
        {"label": "B", "text": "y", "is_correct": True},
    ])
    expl = "Wait—let me reconsider. The correct answer is A, not B."
    ok, reason = validate_explanation(expl, q)
    assert ok is False
    assert "self-correction" in reason


def test_explicit_letter_disagreement_rejected():
    q = _q([
        {"label": "A", "text": "x", "is_correct": False},
        {"label": "C", "text": "z", "is_correct": True},
    ])
    expl = "The correct answer is A because the argument leans on x."
    ok, reason = validate_explanation(expl, q)
    assert ok is False
    assert "states the correct answer is A" in reason


def test_swapped_explanation_rejected():
    """Explanation that has nothing to do with the marked option."""
    q = _q([
        {"label": "A", "text": "ascend rapidly", "is_correct": True},
        {"label": "B", "text": "fall slowly",   "is_correct": False},
    ])
    # Explanation talks about cooking; the marked option is "ascend rapidly".
    expl = "The recipe requires baking the dough until golden brown."
    ok, reason = validate_explanation(expl, q)
    assert ok is False
    assert "doesn't reference" in reason


def test_explanation_word_match_passes():
    q = _q([
        {"label": "A", "text": "ubiquitous", "is_correct": True},
        {"label": "B", "text": "rare",       "is_correct": False},
    ])
    expl = "The blank wants something widespread; ubiquitous fits exactly."
    ok, _ = validate_explanation(expl, q)
    assert ok is True


def test_empty_explanation_rejected():
    q = _q([{"label": "A", "text": "x", "is_correct": True}])
    ok, reason = validate_explanation("", q)
    assert ok is False
    assert "empty" in reason


def test_no_options_passes_through():
    """Numeric / AWA questions have no labelled options — gate is a no-op."""
    q = {"id": 1, "subtype": "numeric_entry", "options": []}
    ok, _ = validate_explanation("Compute 2+3 = 5.", q)
    assert ok is True


def test_no_marked_correct_passes_through():
    """A malformed question (no is_correct flag) isn't the explanation's fault."""
    q = _q([{"label": "A", "text": "x", "is_correct": False}])
    ok, _ = validate_explanation("Any plausible explanation.", q)
    assert ok is True
