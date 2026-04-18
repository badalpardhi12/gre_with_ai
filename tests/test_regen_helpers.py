"""
Unit tests for `scripts.regenerate_retired_answers` helpers.

The end-to-end LLM call is exercised by running the script with
`--dry-run` against the live LLM gateway gateway; this file only covers
pure-Python parsing/normalisation so CI doesn't need network access.
"""
from scripts.regenerate_retired_answers import (
    _normalise_label, _parse_json_strict, _question_to_prompt,
)


def test_normalise_label_extracts_letter_from_prose():
    assert _normalise_label("The answer is B", {"A", "B", "C"}) == "B"


def test_normalise_label_lowercase_input():
    assert _normalise_label("answer: c", {"A", "B", "C"}) == "C"


def test_normalise_label_multi_select_alphabetised():
    assert _normalise_label("C and A", {"A", "B", "C", "D"}) == "A,C"


def test_normalise_label_dedupes():
    assert _normalise_label("A, A, A", {"A", "B"}) == "A"


def test_normalise_label_unknown_returns_none():
    assert _normalise_label("Z", {"A", "B"}) is None


def test_normalise_label_empty_returns_none():
    assert _normalise_label("", {"A"}) is None


def test_parse_json_strict_handles_fences():
    raw = '```json\n{"label": "B", "explanation": "x"}\n```'
    assert _parse_json_strict(raw) == {"label": "B", "explanation": "x"}


def test_parse_json_strict_handles_plain():
    assert _parse_json_strict('{"k": 1}') == {"k": 1}


class _StubQ:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _StubOpt:
    def __init__(self, label, text):
        self.option_label = label
        self.option_text = text


def test_question_to_prompt_includes_options_and_stimulus():
    q = _StubQ(subtype="rc_single", measure="verbal", prompt="Q?", id=1)
    opts = [_StubOpt("A", "first"), _StubOpt("B", "second")]
    stim = _StubQ(stimulus_type="passage", content="A passage about ducks.")
    out = _question_to_prompt(q, opts, stim)
    assert "Subtype: rc_single" in out
    assert "Measure: verbal" in out
    assert "A passage about ducks." in out
    assert "A) first" in out
    assert "B) second" in out


def test_question_to_prompt_no_stimulus_no_options():
    q = _StubQ(subtype="numeric_entry", measure="quant", prompt="Compute.", id=1)
    out = _question_to_prompt(q, [], None)
    assert "Compute." in out
    assert "Stimulus" not in out
    assert "Options" not in out
