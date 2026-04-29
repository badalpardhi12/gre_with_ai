"""Regression tests for the kaplan_sample_review markdown renderer.

Covers two render-layer defects raised in the user's review:

* Q9 (`chapter15:set1:q3`, "area of a circle is 36"): the source has no
  MCQ options because the publisher's question is a free-response
  short-answer with the printed answer rendered as a glyph image
  (\\(\\frac{12}{\\sqrt{\\pi}}\\)). The previous renderer surfaced the
  generic ``_(no options extracted)_`` message, which read like a parser
  drop. The renderer now distinguishes ``mcq_short_answer`` and surfaces
  the symbolic expected answer in inline-math mode.

* Q16 (`chapter18:set1:q1`, DI percent-change explanation): the
  explanation prose contains six bare currency ``$`` glyphs (``$675``,
  ``$750`` etc.) that any MathJax/KaTeX-aware renderer treats as
  math-mode toggles, mangling the prose between every ``$`` pair. The
  renderer now post-processes prose to escape stray ``$`` characters
  while leaving ``\\(...\\)`` LaTeX blocks intact.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest


REPO = "/Users/chiku/Documents/side_projects/gre_with_ai"
RENDERER = os.path.join(REPO, "kaplan_sample_review_tmp", "build_markdown.py")


def _load_renderer():
    """Load the renderer module by file path (it lives outside the
    package tree because it's a one-shot build helper)."""
    if not os.path.exists(RENDERER):
        pytest.skip(f"renderer not present at {RENDERER}")
    spec = importlib.util.spec_from_file_location(
        "kaplan_review_renderer", RENDERER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kaplan_review_renderer"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Q16: prose-dollar escaping (math-mode toggle defect) ───────────


def test_dollar_outside_math_is_escaped():
    """Bare currency ``$`` in prose must be escaped to ``\\$`` so it
    doesn't toggle MathJax/KaTeX math mode."""
    bm = _load_renderer()
    s = "earnings of $675 and $750"
    out = bm._escape_dollars_outside_math(s)
    # Both currency tokens are escaped (so no bare $ remains).
    import re
    assert re.search(r"(?<!\\)\$\d", out) is None
    assert "\\$675" in out
    assert "\\$750" in out


def test_dollar_inside_math_block_left_alone():
    """``\\$`` inside an inline ``\\(...\\)`` block is the legitimate
    math-mode escape and must not be touched (no double-escape)."""
    bm = _load_renderer()
    s = r"the ratio is \(\frac{\$75}{\$750}\) here"
    out = bm._escape_dollars_outside_math(s)
    # Inside the math block, the original \$ stays single-escaped.
    assert r"\(\frac{\$75}{\$750}\)" in out
    # No \\$ (double escape) anywhere.
    assert r"\\$" not in out


def test_already_escaped_dollar_not_double_escaped():
    """``\\$`` already in prose stays as ``\\$``."""
    bm = _load_renderer()
    s = r"price is \$10 today"
    out = bm._escape_dollars_outside_math(s)
    assert out.count(r"\$") == 1
    assert r"\\$" not in out


def test_q16_full_explanation_round_trip():
    """The exact Q16 explanation prose: every prose ``$`` becomes
    ``\\$``, the embedded LaTeX block stays intact, and no math-toggle
    pairs remain."""
    bm = _load_renderer()
    expl = (
        "Referring to the first bar chart, the average weekly earnings "
        "of a high school graduate are $675 and those of a worker with "
        "some college are $750. So the difference in weekly earnings is "
        "$750 - $675 = $75. Since the comparison is being made to the "
        "worker with some college, the percentage change is "
        r"\(\frac{\$75}{\$750} \times 100\% = 10\%\)"
        ", which is (B), the correct answer. If you used $675 as the "
        "denominator, you would have chosen (C), 11%."
    )
    out = bm._escape_dollars_outside_math(expl)
    # Every prose currency mention is escaped.
    for amount in ("\\$675", "\\$750", "\\$75"):
        assert amount in out, f"missing {amount!r} in {out!r}"
    # The LaTeX block survives untouched.
    assert r"\(\frac{\$75}{\$750} \times 100\% = 10\%\)" in out
    # No raw $NNN currency tokens remain in prose.
    import re
    bare = re.findall(r"(?<!\\)\$\d", out)
    # The only $digit allowed is inside the \(...\) block.
    # Strip the math block then check.
    stripped = re.sub(r"\\\(.*?\\\)", "", out, flags=re.S)
    bare = re.findall(r"(?<!\\)\$\d", stripped)
    assert bare == [], f"unescaped prose currency tokens left: {bare}"


# ── Q9: short-answer options + symbolic expected-answer rendering ──


def test_short_answer_options_label_clear():
    """A short-answer item with no options shows a clear free-response
    label, not the generic ``_(no options extracted)_`` text that
    looked like a parser drop."""
    bm = _load_renderer()
    item = {
        "subtype": "mcq_short_answer",
        "options": [],
        "correct_label": r"\frac{12}{\sqrt{\pi}}",
    }
    out = bm.render_options(item, asset_map={})
    assert "no options extracted" not in out
    assert "short answer" in out.lower()
    assert "expected answer" in out.lower()


def test_numeric_entry_label_unchanged():
    """``numeric_entry`` items keep their existing label so the Q9 fix
    doesn't bleed across subtypes."""
    bm = _load_renderer()
    item = {"subtype": "numeric_entry", "options": []}
    out = bm.render_options(item, asset_map={})
    assert "numeric entry" in out
    assert "no multiple choice" in out


def test_render_correct_uses_latex_for_symbolic_answer():
    """When the correct-label string contains LaTeX commands (e.g.,
    ``\\frac{12}{\\sqrt{\\pi}}``), it should render inside an inline-
    math block so MathJax/KaTeX displays it instead of showing the raw
    backslash sequence in a code span."""
    bm = _load_renderer()
    item = {"correct_label": r"\frac{12}{\sqrt{\pi}}"}
    out = bm.render_correct(item)
    assert out.startswith(r"\(")
    assert out.endswith(r"\)")
    assert r"\frac{12}{\sqrt{\pi}}" in out


def test_render_correct_keeps_letter_label_in_code_span():
    """A plain MCQ correct label like ``B`` keeps the existing backtick
    rendering."""
    bm = _load_renderer()
    item = {"correct_label": "B"}
    out = bm.render_correct(item)
    assert out == "`B`"


def test_render_correct_handles_missing_label():
    """A missing correct label shouldn't crash — empty backticks are
    fine."""
    bm = _load_renderer()
    out = bm.render_correct({})
    assert out == "``"
