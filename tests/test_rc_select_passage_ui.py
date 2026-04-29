"""
Tests for the rc_select_passage UI helpers in QuestionScreen.

The screen's passage-annotation / sentence-extraction helpers are plain text
processing (no wx event loop needed); we only pay the wx import cost for the
class namespace. If this file becomes a performance hazard the helpers can
be lifted to a module-level function — they already don't touch `self`.

The whole module is skipped on headless CI (no wxPython installed) because
QuestionScreen subclasses wx.Panel and can't be imported without wx.
"""
import pytest

# Skip the entire file when wxPython is absent (headless CI). Evaluated
# at collection time so pytest records "skipped" rather than "error"
# when the wx-dependent `from screens.question_screen import …` in
# qs_module would otherwise blow up.
pytest.importorskip("wx", reason="QuestionScreen requires wxPython")


@pytest.fixture(scope="module")
def qs_module():
    """Import QuestionScreen once so every test shares the same wx import."""
    from screens.question_screen import QuestionScreen
    return QuestionScreen


def test_extract_passage_sentences_with_sent_tags(qs_module):
    html = (
        "<p><sent id='1'>First sentence.</sent> "
        "<sent id='2'>Second sentence.</sent> "
        "<sent id='3'>Third sentence.</sent></p>"
    )
    out = qs_module._extract_passage_sentences(html)
    assert out == {
        "1": "First sentence.",
        "2": "Second sentence.",
        "3": "Third sentence.",
    }


def test_extract_passage_sentences_double_quoted_ids(qs_module):
    """Both single- and double-quoted `id` forms are in the bank."""
    html = '<sent id="1">Alpha.</sent><sent id="2">Beta.</sent>'
    out = qs_module._extract_passage_sentences(html)
    assert out == {"1": "Alpha.", "2": "Beta."}


def test_extract_passage_sentences_strips_inner_tags(qs_module):
    html = "<sent id='1'>Inner <em>emphasis</em> word.</sent>"
    out = qs_module._extract_passage_sentences(html)
    assert out == {"1": "Inner emphasis word."}


def test_extract_passage_sentences_no_tags_returns_empty(qs_module):
    assert qs_module._extract_passage_sentences("<p>Plain text.</p>") == {}
    assert qs_module._extract_passage_sentences("") == {}
    assert qs_module._extract_passage_sentences(None) == {}


def test_annotate_passage_sentences_injects_numbers(qs_module):
    """Visible `[N]` markers replace the otherwise-stripped `<sent>` tags."""
    html = "<p><sent id='1'>Alpha.</sent> <sent id='2'>Beta.</sent></p>"
    out = qs_module._annotate_passage_sentences(html)
    # Sentence markers should be visible
    assert "[1]" in out
    assert "[2]" in out
    assert "Alpha." in out
    assert "Beta." in out
    # Original `<sent>` tags should be gone
    assert "<sent" not in out


def test_annotate_passage_sentences_passthrough_when_no_tags(qs_module):
    # Passages that were never tagged still render as-is.
    html = "<p>Untagged passage.</p>"
    assert qs_module._annotate_passage_sentences(html) == html


def test_rc_select_passage_round_trip_with_scoring(qs_module):
    """End-to-end: extract sentence N, ensure it matches the correct
    option label exactly so the scorer's `selected_sentence == label`
    comparison succeeds."""
    from services.scoring import ScoringEngine

    passage = (
        "<p><sent id='1'>Intro.</sent> "
        "<sent id='2'>Objection raised here.</sent> "
        "<sent id='3'>Conclusion.</sent></p>"
    )
    sentences = qs_module._extract_passage_sentences(passage)
    assert "2" in sentences
    assert sentences["2"] == "Objection raised here."

    question = {
        "subtype": "rc_select_passage",
        "options": [
            {"label": "1", "text": "Sentence 1", "is_correct": False},
            {"label": "2", "text": "Sentence 2", "is_correct": True},
            {"label": "3", "text": "Sentence 3", "is_correct": False},
        ],
    }
    # UI records `selected_sentence: <option label>` when the user clicks
    # the radio for sentence 2. Scoring should confirm correctness.
    assert ScoringEngine.check_answer(
        question, {"selected_sentence": "2"}
    ) is True
    assert ScoringEngine.check_answer(
        question, {"selected_sentence": "1"}
    ) is False
