"""
Tests for services.figure_mispair_audit (Bug 2).
"""
import base64
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.figure_mispair_audit import (
    MispairJudgment,
    audit_pair,
    build_user_message,
    extract_first_image,
    parse_mispair_response,
    run_single_judge,
)


# ── Image extraction ───────────────────────────────────────────────────


def test_extract_first_image_handles_png():
    fake_png = base64.b64encode(b"\x89PNG\x00mock").decode()
    html = f'<img src="data:image/png;base64,{fake_png}" />'
    result = extract_first_image(html)
    assert result is not None
    raw, media_type = result
    assert media_type == "image/png"
    assert raw.startswith(b"\x89PNG")


def test_extract_first_image_handles_gif_jpeg():
    gif = base64.b64encode(b"GIF89a-mock").decode()
    html = f'<img src="data:image/gif;base64,{gif}" />'
    raw, media_type = extract_first_image(html)
    assert media_type == "image/gif"

    jpg = base64.b64encode(b"\xff\xd8\xff").decode()
    html = f'<img src="data:image/jpeg;base64,{jpg}" />'
    raw, media_type = extract_first_image(html)
    assert media_type == "image/jpeg"


def test_extract_first_image_returns_none_when_absent():
    assert extract_first_image("") is None
    assert extract_first_image("<p>plain text</p>") is None
    assert extract_first_image(None) is None


def test_extract_first_image_picks_first_of_multiple():
    one = base64.b64encode(b"one").decode()
    two = base64.b64encode(b"two").decode()
    html = (
        f'<img src="data:image/png;base64,{one}"/>'
        f'<img src="data:image/png;base64,{two}"/>'
    )
    raw, _ = extract_first_image(html)
    assert raw == b"one"


# ── Response parsing ──────────────────────────────────────────────────


def test_parse_mispair_response_clean_match():
    raw = (
        '{"matches": true, "confidence": "high", '
        '"reasoning": "Chart topic matches stem.", "suspicious": []}'
    )
    j = parse_mispair_response("opus_4_7_vision", raw)
    assert j.matches is True
    assert j.confidence == "high"
    assert j.reasoning == "Chart topic matches stem."
    assert j.suspicious == []
    assert j.error is None


def test_parse_mispair_response_clean_mismatch():
    raw = (
        '{"matches": false, "confidence": "high", '
        '"reasoning": "Image shows answer options, not a chart.", '
        '"suspicious": ["looks_like_options"]}'
    )
    j = parse_mispair_response("sonnet_4_6_vision", raw)
    assert j.matches is False
    assert j.confidence == "high"
    assert j.suspicious == ["looks_like_options"]


def test_parse_mispair_response_strips_code_fences():
    raw = '```json\n{"matches": true, "confidence": "medium", "reasoning": "ok"}\n```'
    j = parse_mispair_response("opus_4_7_vision", raw)
    assert j.matches is True
    assert j.confidence == "medium"


def test_parse_mispair_response_no_json_errors():
    raw = "I could not determine the pairing."
    j = parse_mispair_response("opus_4_7_vision", raw)
    assert j.error == "no_json"


def test_parse_mispair_response_tolerates_extra_prose():
    raw = (
        "Here is my verdict:\n"
        '{"matches": false, "confidence": "high", "reasoning": "wrong subject"}\n'
        "(done)"
    )
    j = parse_mispair_response("sonnet_4_6_vision", raw)
    assert j.matches is False
    assert j.confidence == "high"


def test_parse_mispair_response_invalid_confidence_downgrades():
    raw = '{"matches": true, "confidence": "certain", "reasoning": ""}'
    j = parse_mispair_response("opus_4_7_vision", raw)
    # Unknown confidence value must not raise — downgrade silently.
    assert j.matches is True
    assert j.confidence == "low"


def test_parse_mispair_response_missing_matches():
    raw = '{"confidence": "high", "reasoning": "ok"}'
    j = parse_mispair_response("opus_4_7_vision", raw)
    assert j.error == "missing_matches"


# ── Run single judge ──────────────────────────────────────────────────


def test_run_single_judge_success_first_try():
    def _call(system, user, img, mt):
        return '{"matches": true, "confidence": "high", "reasoning": "ok"}'

    j = run_single_judge(
        "opus_4_7_vision", _call, "sys", "user", b"\x00", "image/png",
    )
    assert j.matches is True
    assert j.error is None


def test_run_single_judge_retries_then_succeeds():
    calls = {"n": 0}

    def _call(system, user, img, mt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 504")
        return '{"matches": false, "confidence": "high", "reasoning": "nope"}'

    j = run_single_judge(
        "sonnet_4_6_vision", _call, "sys", "user", b"\x00", "image/png",
    )
    assert calls["n"] == 2
    assert j.matches is False


def test_run_single_judge_two_failures_errors_out():
    def _call(system, user, img, mt):
        raise RuntimeError("upstream dead")

    j = run_single_judge(
        "opus_4_7_vision", _call, "sys", "user", b"\x00", "image/png",
    )
    assert j.matches is None
    assert j.error is not None


# ── audit_pair aggregation ────────────────────────────────────────────


def _stub(response_text: str):
    def _call(system, user, img, mt):
        return response_text
    return _call


def test_audit_pair_clean_match_no_flag():
    v = audit_pair(
        question_id=1, stimulus_id=10,
        stem="Find the area.",
        image_bytes=b"\x00", media_type="image/png",
        opus_call=_stub(
            '{"matches": true, "confidence": "high", "reasoning": "ok"}'),
        sonnet_call=_stub(
            '{"matches": true, "confidence": "high", "reasoning": "ok"}'),
        parallel=False,
    )
    assert v.confirmed_mispair is False
    assert v.tier2_disagreement is False
    assert len(v.judgments) == 2


def test_audit_pair_both_high_mismatch_confirms():
    v = audit_pair(
        question_id=2, stimulus_id=11,
        stem="What is the revenue?",
        image_bytes=b"\x00", media_type="image/png",
        opus_call=_stub(
            '{"matches": false, "confidence": "high", '
            '"reasoning": "shows options grid", '
            '"suspicious": ["looks_like_options"]}'
        ),
        sonnet_call=_stub(
            '{"matches": false, "confidence": "high", '
            '"reasoning": "unrelated chart", '
            '"suspicious": ["wrong_subject"]}'
        ),
        parallel=False,
    )
    assert v.confirmed_mispair is True
    assert v.tier2_disagreement is False


def test_audit_pair_one_only_says_mismatch_is_tier2():
    v = audit_pair(
        question_id=3, stimulus_id=12,
        stem="Compute the median.",
        image_bytes=b"\x00", media_type="image/png",
        opus_call=_stub(
            '{"matches": false, "confidence": "high", "reasoning": "oops"}'),
        sonnet_call=_stub(
            '{"matches": true, "confidence": "medium", "reasoning": "ok"}'),
        parallel=False,
    )
    assert v.confirmed_mispair is False
    assert v.tier2_disagreement is True


def test_audit_pair_mismatch_but_low_confidence_does_not_confirm():
    # Even if both judges say matches=false, low confidence must NOT
    # trigger auto-demotion. Spec: confirmed only when both high.
    v = audit_pair(
        question_id=4, stimulus_id=13,
        stem="Find the mode.",
        image_bytes=b"\x00", media_type="image/png",
        opus_call=_stub(
            '{"matches": false, "confidence": "low", "reasoning": "unsure"}'),
        sonnet_call=_stub(
            '{"matches": false, "confidence": "low", "reasoning": "unsure"}'),
        parallel=False,
    )
    assert v.confirmed_mispair is False
    # Only "high" mismatches count towards tier2 either.
    assert v.tier2_disagreement is False


def test_audit_pair_serializes_to_dict():
    v = audit_pair(
        question_id=5, stimulus_id=14,
        stem="Stem",
        image_bytes=b"\x00", media_type="image/png",
        opus_call=_stub(
            '{"matches": true, "confidence": "high", "reasoning": "ok"}'),
        sonnet_call=_stub(
            '{"matches": true, "confidence": "high", "reasoning": "ok"}'),
        parallel=False,
    )
    d = v.as_dict()
    assert d["question_id"] == 5
    assert d["stimulus_id"] == 14
    assert len(d["judgments"]) == 2
    assert d["confirmed_mispair"] is False


# ── User message shape ────────────────────────────────────────────────


def test_build_user_message_includes_stem():
    msg = build_user_message("Compute X.", subtype="data_interp",
                             source="princeton")
    assert "SUBTYPE: data_interp" in msg
    assert "SOURCE: princeton" in msg
    assert "Compute X." in msg
    assert "JSON only" in msg
