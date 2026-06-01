"""Tests for WS-E (migration 043) remaining data-quality cleanup.

Two surfaces:

* ``services.sanitize.convert_single_dollar_math`` — the single-``$`` →
  ``\\(…\\)`` converter, with heavy emphasis on currency-safety (never
  mangle "$48" or "$5 and $3") and idempotency.
* ``models.migrations._043_data_quality_cleanup_2026_06_01`` — applies the
  converter to the ``unmatched_dollar``-flagged field of each live item and
  reclassifies the mis-typed Kaplan RC stimuli, on a controlled temp_db.
"""
from __future__ import annotations

import sys

import pytest

from services.sanitize import convert_single_dollar_math


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    for mod in [m for m in list(sys.modules)
                if m == "models.migrations" or m.startswith("models.migrations.")]:
        del sys.modules[mod]
    yield


# ── convert_single_dollar_math: math conversion ─────────────────────────


def test_converts_real_latex_span():
    assert convert_single_dollar_math("$x^2$") == "\\(x^2\\)"


def test_converts_frac_span():
    assert (convert_single_dollar_math(r"value is $\frac{1}{2}$ here")
            == r"value is \(\frac{1}{2}\) here")


def test_converts_subscript_and_superscript():
    assert convert_single_dollar_math("$a_1 + b^{2}$") == "\\(a_1 + b^{2}\\)"


def test_leaves_existing_paren_delimiters_untouched():
    src = r"already \(x^2\) good"
    assert convert_single_dollar_math(src) == src


def test_leaves_display_math_untouched():
    src = "$$x^2 + 1$$"
    assert convert_single_dollar_math(src) == src


# ── convert_single_dollar_math: currency safety ─────────────────────────


def test_single_currency_amount_untouched():
    assert convert_single_dollar_math("It costs $48 today.") == "It costs $48 today."


def test_two_currency_amounts_not_merged():
    # The classic corruption: "$5 and $3" must NOT become one \(5 and \)3 span.
    src = "Buy one for $5 and another for $3."
    assert convert_single_dollar_math(src) == src


def test_currency_arithmetic_not_converted():
    # = between dollar amounts must not trigger a math conversion.
    src = "Cumulative = $63 + $64 = $127"
    assert convert_single_dollar_math(src) == src


def test_currency_with_decimals_untouched():
    src = "1% of $450 is $4.50, so 0.5% is $2.25."
    assert convert_single_dollar_math(src) == src


def test_mixed_currency_and_math_only_math_converted():
    # Real 2915-shape: an even run of currency dollars, then a clean math
    # span. The math span isolates and converts; currency stays literal.
    src = ("he gets $1 then $2. The total after week is $2^n - 1$ dollars.")
    out = convert_single_dollar_math(src)
    assert "$1" in out and "$2." in out          # currency intact
    assert "\\(2^n - 1\\)" in out                 # math converted
    assert "$2^n" not in out


def test_odd_currency_parity_leaves_everything_alone():
    # When currency dollars interleave with a math span at ODD parity, a
    # clean pair can't be formed — the converter does NOTHING rather than
    # risk corrupting currency. (Safety over completeness.)
    src = ("he gets $1, plus $2 for a total of $3. "
           "The total is $2^n - 1$ dollars.")
    assert convert_single_dollar_math(src) == src


def test_escaped_dollar_inside_math_preserved():
    # \$ currency inside a real math span survives the round trip.
    src = r"price is $\frac{\$24}{80}$ per word"
    out = convert_single_dollar_math(src)
    assert out == r"price is \(\frac{\$24}{80}\) per word"


# ── convert_single_dollar_math: idempotency / edge cases ─────────────────


def test_idempotent():
    src = r"$x^2$ and a price of $48 and $\frac{1}{2}$"
    once = convert_single_dollar_math(src)
    twice = convert_single_dollar_math(once)
    assert once == twice


def test_none_and_empty():
    assert convert_single_dollar_math(None) == ""
    assert convert_single_dollar_math("") == ""


def test_plain_text_no_dollars_unchanged():
    src = "No math here at all."
    assert convert_single_dollar_math(src) == src


def test_span_across_newline_not_converted():
    # A $ … $ pair straddling a paragraph break is almost always two
    # unrelated currency amounts, not a math span.
    src = "He paid $5\nfor the second $7 item^"
    assert convert_single_dollar_math(src) == src


# ── Migration 043 ───────────────────────────────────────────────────────


def _seed_cleanup_db(temp_db):
    """Two live questions + one mis-typed Kaplan-style stimulus.

    qMath (9601): explanation has a real single-$ math span + currency.
    qStim (9602): rc_single whose stimulus_type='graph' must flip to 'passage'.
    """
    from models.database import Question, Stimulus

    qMath = Question.create(
        id=9601, measure="quant", subtype="qc",
        prompt="compare", source="manhattan_5lb_2018", status="live",
        explanation=r"Evaluate $2^3 = 8$. The fee was $48.",
    )

    stim = Stimulus.create(stimulus_type="graph",
                           content="The advent of online education ...")
    qStim = Question.create(
        id=9602, measure="verbal", subtype="rc_single",
        prompt="passage Q", source="kaplan_2024", status="live",
        explanation="", stimulus=stim,
    )
    return qMath, qStim, stim


def test_043_registered(temp_db):
    import models.migrations as m
    applied = {r.name for r in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "043_data_quality_cleanup_2026_06_01" in applied


def test_043_converts_unmatched_dollar_field(temp_db):
    from models.database import Question
    import models.migrations as m
    qMath, _, _ = _seed_cleanup_db(temp_db)
    m._043_data_quality_cleanup_2026_06_01()
    expl = Question.get_by_id(qMath.id).explanation
    assert r"\(2^3 = 8\)" in expl       # math span converted
    assert "$48" in expl                 # currency untouched


def test_043_reclassifies_kaplan_stimulus(temp_db, monkeypatch):
    from models.database import Stimulus
    import models.migrations as m
    _, qStim, stim = _seed_cleanup_db(temp_db)
    # Point the migration's hard-coded qid list at our fixture's qid.
    monkeypatch.setattr(m, "_KAPLAN_PASSAGE_STIMULI_2026_06_01", (qStim.id,))
    m._043_data_quality_cleanup_2026_06_01()
    assert Stimulus.get_by_id(stim.id).stimulus_type == "passage"


def test_043_idempotent(temp_db, monkeypatch):
    from models.database import Question, Stimulus
    import models.migrations as m
    qMath, qStim, stim = _seed_cleanup_db(temp_db)
    monkeypatch.setattr(m, "_KAPLAN_PASSAGE_STIMULI_2026_06_01", (qStim.id,))
    m._043_data_quality_cleanup_2026_06_01()
    first_expl = Question.get_by_id(qMath.id).explanation
    m._043_data_quality_cleanup_2026_06_01()  # second pass
    assert Question.get_by_id(qMath.id).explanation == first_expl
    assert Stimulus.get_by_id(stim.id).stimulus_type == "passage"


def test_043_leaves_currency_only_explanation_alone(temp_db):
    from models.database import Question
    import models.migrations as m
    q = Question.create(
        id=9603, measure="quant", subtype="qc", prompt="p",
        source="manhattan_5lb_2018", status="live",
        explanation="Week 1: $1; Week 2: $1 + $1 = $2; total $3.",
    )
    m._043_data_quality_cleanup_2026_06_01()
    # No math signature -> nothing converted; currency text byte-identical.
    assert Question.get_by_id(q.id).explanation == \
        "Week 1: $1; Week 2: $1 + $1 = $2; total $3."
