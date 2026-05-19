"""Unit tests for ``services.sanitize`` (Phase 3.3 LaTeX/HTML/MCQ
encoding audit).

These tests cover the public surface:
    * ``find_latex_encoding_issues`` detects each documented hazard.
    * ``find_mcq_option_issues`` detects mixed-prefix conventions
      and accepts uniform sets.
    * ``auto_fix_text`` repairs known-fixable cases (and is idempotent).
    * ``sanitize_html`` parity between strict (raises) and lenient
      (auto-fixes) modes.
    * The fix-script applies a fixable issue end-to-end against a
      temp DB.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

from services.sanitize import (
    ISSUE_MIXED_DELIMITERS,
    ISSUE_MIXED_MCQ_PREFIX,
    ISSUE_NBSP_IN_MATH,
    ISSUE_SMART_QUOTES_IN_MATH,
    ISSUE_TRIPLE_DOLLAR,
    ISSUE_UNESCAPED_HTML,
    ISSUE_UNMATCHED_DOLLAR,
    LatexEncodingError,
    auto_fix_text,
    find_latex_encoding_issues,
    find_mcq_option_issues,
    sanitize_html,
)


# ── find_latex_encoding_issues ────────────────────────────────────────


def test_empty_input_returns_empty_list():
    assert find_latex_encoding_issues("") == []
    assert find_latex_encoding_issues(None) == []


def test_clean_text_returns_empty_list():
    """Pure prose with valid LaTeX shouldn't flag anything."""
    assert find_latex_encoding_issues(
        r"The value is \(\frac{1}{2}\), Quantity A is greater."
    ) == []
    assert find_latex_encoding_issues("plain prose with no math.") == []


def test_smart_quotes_in_math_detected():
    """Curly quote inside an inline-math span breaks KaTeX."""
    issues = find_latex_encoding_issues(
        r"the result \(\frac{a}{b‘}\) is wrong"
    )
    types = {t for t, _ in issues}
    assert ISSUE_SMART_QUOTES_IN_MATH in types


def test_smart_quotes_outside_math_not_flagged():
    """Curly quote in prose is fine — it's only a hazard inside
    KaTeX-parsed spans."""
    issues = find_latex_encoding_issues(
        "the senator's memoir, said the author."
    )
    types = {t for t, _ in issues}
    assert ISSUE_SMART_QUOTES_IN_MATH not in types


def test_unmatched_dollar_detected():
    """Odd number of bare ``$`` is an inline-math parity break."""
    issues = find_latex_encoding_issues("compute $x^2 + y^2 over R")
    types = {t for t, _ in issues}
    assert ISSUE_UNMATCHED_DOLLAR in types


def test_balanced_dollar_not_flagged():
    """``$x^2 + y^2$`` is a perfectly valid inline-math span."""
    issues = find_latex_encoding_issues("compute $x^2 + y^2$ over R")
    types = {t for t, _ in issues}
    assert ISSUE_UNMATCHED_DOLLAR not in types


def test_dollar_amount_not_flagged():
    """Currency-style ``$5 per item`` must not trip the parity check."""
    issues = find_latex_encoding_issues("the cost is $5 per item")
    types = {t for t, _ in issues}
    assert ISSUE_UNMATCHED_DOLLAR not in types


def test_mixed_delimiters_detected():
    """Both ``\\(...\\)`` and bare ``$...$`` in the same string."""
    issues = find_latex_encoding_issues(
        r"a \(\frac{1}{2}\) and $\frac{3}{4}$ side by side"
    )
    types = {t for t, _ in issues}
    assert ISSUE_MIXED_DELIMITERS in types


def test_paren_only_not_mixed():
    """Pure ``\\(...\\)`` should NOT flag mixed-delimiter."""
    issues = find_latex_encoding_issues(
        r"a \(\frac{1}{2}\) and \(\frac{3}{4}\)"
    )
    types = {t for t, _ in issues}
    assert ISSUE_MIXED_DELIMITERS not in types


def test_nbsp_in_math_detected():
    """U+00A0 inside a math span breaks KaTeX's lexer."""
    issues = find_latex_encoding_issues(
        "answer is \\(\\frac{1}{2} \\) cm"
    )
    types = {t for t, _ in issues}
    assert ISSUE_NBSP_IN_MATH in types


def test_triple_dollar_detected():
    issues = find_latex_encoding_issues("equation $$$x^2$$$ end")
    types = {t for t, _ in issues}
    assert ISSUE_TRIPLE_DOLLAR in types


def test_bare_ampersand_detected():
    """Bare ``&`` not part of an entity is unescaped HTML."""
    issues = find_latex_encoding_issues("factors of 27 are 1 & 27, 3 & 9")
    types = {t for t, _ in issues}
    assert ISSUE_UNESCAPED_HTML in types


def test_valid_html_tags_not_flagged():
    """Properly-formed HTML tags should not trigger the unescaped-HTML
    detector."""
    issues = find_latex_encoding_issues(
        "<p>Quantity A: <strong>\\(\\frac{1}{2}\\)</strong></p>"
    )
    types = {t for t, _ in issues}
    assert ISSUE_UNESCAPED_HTML not in types


def test_math_comparison_in_prose_not_flagged():
    """``a > b`` and ``x < 5`` in prose are valid, not hazards."""
    issues = find_latex_encoding_issues(
        r"Since \(0.4545... > 0.45\), Quantity A is greater."
    )
    types = {t for t, _ in issues}
    assert ISSUE_UNESCAPED_HTML not in types


# ── find_mcq_option_issues ───────────────────────────────────────────


def test_mcq_uniform_paren_no_flag():
    options = ["(A) hello", "(B) world", "(C) foo"]
    assert find_mcq_option_issues(options) == []


def test_mcq_uniform_dot_no_flag():
    options = ["A. hello", "B. world", "C. foo"]
    assert find_mcq_option_issues(options) == []


def test_mcq_uniform_no_prefix_no_flag():
    options = ["hello", "world", "foo"]
    assert find_mcq_option_issues(options) == []


def test_mcq_mixed_prefix_flagged():
    options = ["(A) hello", "B. world", "C) foo"]
    issues = find_mcq_option_issues(options)
    types = {t for t, _ in issues}
    assert ISSUE_MIXED_MCQ_PREFIX in types


def test_mcq_paren_plus_unprefixed_flagged():
    options = ["(A) hello", "world without prefix", "(C) foo"]
    issues = find_mcq_option_issues(options)
    types = {t for t, _ in issues}
    assert ISSUE_MIXED_MCQ_PREFIX in types


def test_mcq_single_option_no_flag():
    """Trivially-uniform 1-element sets shouldn't flag."""
    assert find_mcq_option_issues(["(A) hello"]) == []
    assert find_mcq_option_issues([]) == []


# ── auto_fix_text ────────────────────────────────────────────────────


def test_auto_fix_triple_dollar():
    out, applied = auto_fix_text("$$$math$$$")
    assert out == "$$math$$"
    assert ISSUE_TRIPLE_DOLLAR in applied


def test_auto_fix_smart_quote_in_math_only():
    """Smart quote inside a math span is repaired; identical character
    in narrative prose is left alone."""
    out, applied = auto_fix_text(
        "the senator's \\(\\frac{a}{b‘}\\) memoir"
    )
    assert ISSUE_SMART_QUOTES_IN_MATH in applied
    # Quote in prose is preserved.
    assert "senator's" in out
    # Quote in math is fixed.
    assert "‘" not in out.split("\\)")[0]


def test_auto_fix_nbsp_in_math():
    out, applied = auto_fix_text("\\(\\frac{1}{2} \\) cm")
    assert ISSUE_NBSP_IN_MATH in applied
    assert " " not in out


def test_auto_fix_mixed_delimiters_canonicalises():
    """When BOTH ``\\(...\\)`` and ``$...$`` appear, the mixed pair
    is canonicalised to ``\\(...\\)``."""
    out, applied = auto_fix_text(
        r"a \(\frac{1}{2}\) and $\frac{3}{4}$ side by side"
    )
    assert ISSUE_MIXED_DELIMITERS in applied
    assert r"\(\frac{3}{4}\)" in out
    assert r"$\frac{3}{4}$" not in out


def test_auto_fix_idempotent():
    """Applying the fix twice produces the same result as applying once."""
    raw = (
        r"$$$math$$$ a \(\frac{1}{2}\) and $x^2$ "
        "and \\(\\frac{1}{4} \\)"
    )
    out1, _ = auto_fix_text(raw)
    out2, _ = auto_fix_text(out1)
    assert out1 == out2


def test_auto_fix_clean_text_noop():
    raw = r"\(\frac{1}{2}\) plus a literal $5"
    out, applied = auto_fix_text(raw)
    assert out == raw
    assert applied == []


# ── sanitize_html ────────────────────────────────────────────────────


def test_sanitize_html_lenient_returns_string():
    out = sanitize_html("<p>hello</p>", mode="lenient")
    assert "<p>" in out
    assert "hello" in out


def test_sanitize_html_strict_raises_on_hazard():
    with pytest.raises(LatexEncodingError):
        sanitize_html("$$$bad$$$", mode="strict")


def test_sanitize_html_strict_passes_clean():
    out = sanitize_html("<p>plain text</p>", mode="strict")
    assert "<p>plain text</p>" == out


def test_sanitize_html_lenient_auto_fixes():
    """Lenient mode runs auto-fix and then sanitises — output should
    no longer contain the original triple-dollar."""
    out = sanitize_html("$$$math$$$", mode="lenient")
    assert "$$$" not in out


def test_sanitize_html_invalid_mode():
    with pytest.raises(ValueError):
        sanitize_html("hi", mode="bogus")


def test_sanitize_html_none_input():
    assert sanitize_html(None) == ""
    assert sanitize_html("") == ""


# ── End-to-end: fix script applies a known-fixable case ──────────────


def test_fix_script_applies_known_case(temp_db, tmp_path):
    """Stage a question with a triple-dollar hazard, run the fix
    script with --apply, and confirm the field was rewritten and the
    flag NOT recorded (because the issue was fixable, not review-only)."""
    from models.database import Question, QuestionFlag

    # 1) Create a question with a fixable issue.
    q = Question.create(
        measure="quant",
        subtype="qc",
        prompt="Compute $$$math$$$ at the end.",
        explanation="The price was $5 plus another $5 totaling $10.",
        topic="arithmetic",
    )

    # 2) Stage a CSV mirroring what audit_encoding_issues.py would
    #    produce.
    csv_path = tmp_path / "encoding_issues_test.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["qid", "field", "issue_type", "snippet"])
        w.writerow([q.id, "prompt", ISSUE_TRIPLE_DOLLAR, "$$$math$$$"])

    # 3) Run the fix script with --apply.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.fix_encoding_issues import main as fix_main
    rc = fix_main(["--csv", str(csv_path), "--apply"])
    assert rc == 0

    # 4) Verify the prompt is rewritten.
    q2 = Question.get_by_id(q.id)
    assert "$$$" not in q2.prompt
    assert "$$math$$" in q2.prompt

    # 5) The fix is auto-fixable, so no review flag should be added.
    assert QuestionFlag.select().where(
        QuestionFlag.question == q
    ).count() == 0


def test_fix_script_records_flag_for_review_only_issue(temp_db, tmp_path):
    """An ``unmatched_dollar`` is review-only (auto-fix would corrupt
    the math). The fix script must record a ``QuestionFlag`` rather
    than rewriting."""
    from models.database import Question, QuestionFlag

    q = Question.create(
        measure="quant",
        subtype="qc",
        prompt="some prompt",
        explanation="open math $x^2 + y^2 with no close",
        topic="arithmetic",
    )

    csv_path = tmp_path / "encoding_issues_test.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["qid", "field", "issue_type", "snippet"])
        w.writerow([
            q.id, "explanation", ISSUE_UNMATCHED_DOLLAR,
            "open math $x^2 + y^2 with no close",
        ])

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.fix_encoding_issues import main as fix_main
    rc = fix_main(["--csv", str(csv_path), "--apply"])
    assert rc == 0

    # Explanation NOT rewritten.
    q2 = Question.get_by_id(q.id)
    assert q2.explanation == "open math $x^2 + y^2 with no close"

    # Flag recorded.
    flags = list(
        QuestionFlag.select().where(QuestionFlag.question == q)
    )
    assert len(flags) == 1
    assert "encoding_issue" in flags[0].note
    assert flags[0].user_id == "encoding_audit"


def test_fix_script_idempotent(temp_db, tmp_path):
    """Running the fix script twice produces no net change after the
    first apply."""
    from models.database import Question, QuestionFlag

    q = Question.create(
        measure="quant",
        subtype="qc",
        prompt="Compute $$$x$$$.",
        topic="arithmetic",
    )

    csv_path = tmp_path / "encoding_issues_test.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["qid", "field", "issue_type", "snippet"])
        w.writerow([q.id, "prompt", ISSUE_TRIPLE_DOLLAR, "$$$"])

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.fix_encoding_issues import main as fix_main

    # First apply.
    fix_main(["--csv", str(csv_path), "--apply"])
    after_first = Question.get_by_id(q.id).prompt

    # Second apply — same CSV, no further change.
    fix_main(["--csv", str(csv_path), "--apply"])
    after_second = Question.get_by_id(q.id).prompt

    assert after_first == after_second
    # And no duplicate flag rows since the auto-fixer succeeded.
    assert QuestionFlag.select().where(
        QuestionFlag.question == q
    ).count() == 0
