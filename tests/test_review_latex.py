"""Regression tests for LaTeX rendering in the post-section review
dialog (`screens.answer_review_dialog.AnswerReviewDialog`).

Bug: the dialog used plain ``wx.StaticText`` for prompt, options, and
explanation, so items whose content had LaTeX inline-math (e.g.
``\\(93\\frac{1}{3}°\\)``) showed up as raw macros. The live question
screen already normalises option labels through
``widgets.latex_inline_text.latex_inline_to_text``; this test pins down
that the review dialog now runs every user-facing string through the
same helper so the rendered ``wx.StaticText`` labels contain no raw
``\\(`` / ``\\frac{`` / ``$…math…$`` fragments.
"""
import pytest
import wx


@pytest.fixture(scope="module")
def _wx_app():
    """Headless wx.App — scoped to the module so we don't repeatedly
    construct/destruct the native app (flaky on macOS CI)."""
    app = wx.App(False)
    yield app


def _collect_static_text_labels(widget):
    """Walk the widget tree under *widget* and return every
    ``wx.StaticText`` label as a list, in visual (depth-first) order."""
    out = []
    stack = [widget]
    while stack:
        w = stack.pop(0)
        if isinstance(w, wx.StaticText):
            out.append(w.GetLabel())
        if hasattr(w, "GetChildren"):
            stack.extend(list(w.GetChildren()))
    return out


def _assert_no_raw_latex(labels):
    """Fail if any label still contains raw LaTeX tokens that the
    KaTeX-free StaticText can't render. We check the tokens that the
    user actually reported seeing rendered as source text."""
    forbidden_tokens = [r"\frac{", r"\dfrac{", r"\tfrac{", r"\("]
    for L in labels:
        for tok in forbidden_tokens:
            assert tok not in L, (
                f"Raw LaTeX token {tok!r} still present in review label: {L!r}"
            )


def test_dialog_normalises_latex_in_prompt_option_and_explanation(_wx_app):
    """Regression for the reported Q27 / qid=1663 bug: prompt,
    options, and the explanation all contained ``\\(…\\frac{…}{…}…\\)``
    patterns that showed up raw. The dialog must normalise them into
    Unicode-fractions via ``latex_inline_to_text``."""
    from screens.answer_review_dialog import AnswerReviewDialog

    details = [{
        "question_id": 1663,
        "measure": "quant",
        "subtype": "mcq_single",
        "prompt": r"Find angle C where \(C = 46\frac{2}{3}°\).",
        "options": [
            {"label": "A", "text": r"\(46\frac{2}{3}°\)", "is_correct": False},
            {"label": "B", "text": r"\(70°\)", "is_correct": False},
            {"label": "C", "text": r"\(80°\)", "is_correct": False},
            {"label": "D", "text": r"\(93\frac{1}{3}°\)", "is_correct": True},
            {"label": "E", "text": r"100°", "is_correct": False},
        ],
        "explanation": (
            r"Because \(C = 46\frac{2}{3}°\), the supplementary angle is "
            r"\(93\frac{1}{3}°\)."
        ),
        "is_correct": False,
        "user_response": {"selected": ["E"]},
    }]
    dlg = AnswerReviewDialog(None, details)
    try:
        labels = _collect_static_text_labels(dlg)
        _assert_no_raw_latex(labels)

        # Positive check: the Unicode fraction slash (U+2044) rendered
        # for the "simple numerator/denominator" case the helper uses.
        joined = "\n".join(labels)
        assert "2⁄3" in joined, (
            "Expected the \\frac{2}{3} in the prompt to render as Unicode "
            "2⁄3 via latex_inline_to_text; labels were:\n"
            + joined
        )
        assert "1⁄3" in joined, (
            "Expected the \\frac{1}{3} in option D / explanation to render "
            "as Unicode 1⁄3; labels were:\n" + joined
        )
    finally:
        dlg.Destroy()


def test_dialog_normalises_dollar_delimited_math(_wx_app):
    """Regression for the reported Q23 / qid=4472 bug: the explanation
    used single-``$`` math delimiters (``$\\frac{4^2}{2^4}$``) which
    the original helper left intact. Both delimiters and inner macros
    must be rewritten, without eating literal dollar amounts elsewhere
    in the same string."""
    from screens.answer_review_dialog import AnswerReviewDialog

    details = [{
        "question_id": 4472,
        "measure": "quant",
        "subtype": "mcq_single",
        "prompt": "Compute the ratio.",
        "options": [
            {"label": "A", "text": r"$\frac{4^2}{2^4}$", "is_correct": True},
            {"label": "B", "text": "The item costs $5 and rises by $2.",
             "is_correct": False},
        ],
        "explanation": (
            r"We have $\frac{4^2}{2^4} = 1$. Compare to the $5 item."
        ),
        "is_correct": True,
        "user_response": {"selected": ["A"]},
    }]
    dlg = AnswerReviewDialog(None, details)
    try:
        labels = _collect_static_text_labels(dlg)
        _assert_no_raw_latex(labels)
        joined = "\n".join(labels)

        # The math fraction rendered.
        assert "(4²)/(2⁴)" in joined, (
            "Expected $\\frac{4^2}{2^4}$ to unwrap and render as "
            "(4²)/(2⁴); labels were:\n" + joined
        )
        # Literal dollar amounts survived (they were outside a
        # math-signature pair and so should be preserved).
        assert "$5" in joined, (
            "Literal dollar amount `$5` was eaten by the math-delimiter "
            "stripper; labels were:\n" + joined
        )
    finally:
        dlg.Destroy()


def test_correct_answer_summary_row_normalises_latex(_wx_app):
    """The "Correct answer: …" summary row runs through
    ``_format_correct_answer`` which truncates the option text to 80
    chars. The truncation must happen *after* LaTeX normalisation,
    otherwise long ``\\frac{…}{…}`` macros get mid-escape-sliced and
    end up even more unreadable than the raw source."""
    from screens.answer_review_dialog import (
        AnswerReviewDialog, _format_correct_answer,
    )

    detail = {
        "subtype": "mcq_single",
        "options": [
            {"label": "D", "text": r"\(93\frac{1}{3}°\)", "is_correct": True},
        ],
    }
    summary = _format_correct_answer(detail)
    assert r"\frac" not in summary
    assert r"\(" not in summary
    assert "1⁄3" in summary, (
        f"Correct-answer summary should carry the Unicode fraction; "
        f"got {summary!r}"
    )
