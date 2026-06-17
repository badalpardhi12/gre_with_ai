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


def test_select_in_passage_native_round_trip():
    """The ETS select-in-passage mechanic builds clickable sentence rows in
    the passage pane; clicking one records ``selected_sentence`` = its label,
    which the scorer then matches against the correct option label."""
    import wx
    from screens.question_screen import QuestionScreen
    from services.scoring import ScoringEngine

    app = wx.App(False)  # noqa: F841 — keep alive for widget construction
    frame = wx.Frame(None)
    screen = QuestionScreen(frame)

    class _FakeSS:
        def __init__(self, qid):
            self.current_question_id = qid
            self.question_ids = [qid]
            self.current_index = 0
            self.total_questions = 1
            self.responses = {}
            self.marked = set()

        def set_response(self, qid, payload):
            self.responses[qid] = payload

        def get_response(self, qid):
            return self.responses.get(qid)

    q = {
        "id": 99,
        "subtype": "rc_select_passage",
        "prompt": "Click the sentence that raises the objection.",
        "stimulus": {"content": (
            "<p><sent id='1'>Intro.</sent> "
            "<sent id='2'>Objection raised here.</sent> "
            "<sent id='3'>Conclusion.</sent></p>")},
        "options": [
            {"label": "1", "text": "Sentence 1", "is_correct": False},
            {"label": "2", "text": "Sentence 2", "is_correct": True},
            {"label": "3", "text": "Sentence 3", "is_correct": False},
        ],
    }
    screen._current_q = q
    screen._section_state = _FakeSS(q["id"])
    screen._build_select_in_passage(q, q["options"])

    # Three clickable sentence rows built in the passage pane.
    sip = [e for e in screen._answer_controls if e[0] == "sip"]
    assert [e[1] for e in sip] == ["1", "2", "3"]

    # Click sentence 2 → payload + scoring agree.
    screen._on_sip_pick("2", sip[1][2])
    assert screen._get_current_response() == {"selected_sentence": "2"}
    assert ScoringEngine.check_answer(q, screen._get_current_response()) is True

    frame.Destroy()


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
