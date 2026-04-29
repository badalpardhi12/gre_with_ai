"""Unit tests for services.vision_expert_review.

All tests stub the multi-modal judge calls — no network, no gateway,
no real image decoding beyond round-tripping a tiny byte string.
"""
from __future__ import annotations

import json

import pytest

from services import vision_expert_review as vr


FAKE_IMAGE = b"\x47\x49\x46\x38\x39\x61"  # "GIF89a" header

GOOD_QUESTION = {
    "stem": "Just as different people can have varied personalities, so too can pets possess varied _______.",
    "correct_label": "E",
    "explanation": "Temperaments parallels personalities.",
    "subtype": "tc",
    "difficulty_target": 3,
    "source": "princeton_2012",
}


def _fake_call_returning(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def _call(system, user, image_bytes, media_type):
        return text
    return _call


def _judge(name, payload):
    return {"name": name, "call": _fake_call_returning(payload)}


# ── parse ─────────────────────────────────────────────────────────────


def test_parse_clean_json_populates_every_axis():
    raw = json.dumps({
        "scores": {
            "correctness": 5, "clarity": 4, "distractor_quality": 4,
            "difficulty_match": 4, "gre_authenticity": 5,
        },
        "defects": [],
        "notes": "",
        "read_options": {"A": "foo", "B": "bar"},
    })
    rep = vr._parse_vision_response("opus", raw)
    assert rep.error is None
    assert rep.scores == {
        "correctness": 5, "clarity": 4, "distractor_quality": 4,
        "difficulty_match": 4, "gre_authenticity": 5,
    }
    assert rep.read_options == {"A": "foo", "B": "bar"}


def test_parse_strips_markdown_fence():
    raw = "```json\n" + json.dumps({
        "scores": {ax: 4 for ax in vr.VISION_AXES},
    }) + "\n```"
    rep = vr._parse_vision_response("sonnet", raw)
    assert rep.error is None
    assert all(rep.scores[ax] == 4 for ax in vr.VISION_AXES)


def test_parse_handles_leading_prose():
    raw = "Here is my review:\n\n" + json.dumps({
        "scores": {ax: 3 for ax in vr.VISION_AXES},
    })
    rep = vr._parse_vision_response("gemini", raw)
    assert rep.error is None


def test_parse_rejects_non_json_response():
    rep = vr._parse_vision_response("opus", "I cannot review this.")
    assert rep.error == "no_json"
    assert rep.scores == {}


def test_parse_clamps_out_of_range_scores():
    raw = json.dumps({
        "scores": {ax: 99 for ax in vr.VISION_AXES},
    })
    rep = vr._parse_vision_response("sonnet", raw)
    assert all(rep.scores[ax] == 5 for ax in vr.VISION_AXES)


# ── aggregation ──────────────────────────────────────────────────────


def test_aggregate_promotes_when_everyone_agrees_high():
    reports = [
        vr._parse_vision_response(
            name,
            json.dumps({"scores": {ax: 5 for ax in vr.VISION_AXES}}),
        )
        for name in ("opus", "sonnet", "gemini")
    ]
    verdict = vr.aggregate_vision_panel(reports)
    assert verdict["verdict"] == "live"
    assert not verdict["failures"]
    assert not verdict["escalated"]


def test_aggregate_demotes_on_low_majority():
    # correctness: 5,2,2 — only 1 judge >= 4
    mixed = [
        {"correctness": 5, "clarity": 4, "distractor_quality": 4,
         "difficulty_match": 4, "gre_authenticity": 4},
        {"correctness": 2, "clarity": 4, "distractor_quality": 4,
         "difficulty_match": 4, "gre_authenticity": 4},
        {"correctness": 2, "clarity": 4, "distractor_quality": 4,
         "difficulty_match": 4, "gre_authenticity": 4},
    ]
    reports = [
        vr._parse_vision_response(n, json.dumps({"scores": s}))
        for n, s in zip(("a", "b", "c"), mixed)
    ]
    v = vr.aggregate_vision_panel(reports)
    assert v["verdict"] == "draft"
    assert any(f["axis"] == "correctness" for f in v["failures"])


def test_aggregate_escalates_on_high_spread():
    # Spread of 3 on correctness (5 vs 2) even though majority >= 4
    mixed = [
        {"correctness": 5, "clarity": 5, "distractor_quality": 5,
         "difficulty_match": 5, "gre_authenticity": 5},
        {"correctness": 5, "clarity": 5, "distractor_quality": 5,
         "difficulty_match": 5, "gre_authenticity": 5},
        {"correctness": 2, "clarity": 5, "distractor_quality": 5,
         "difficulty_match": 5, "gre_authenticity": 5},
    ]
    reports = [
        vr._parse_vision_response(n, json.dumps({"scores": s}))
        for n, s in zip(("a", "b", "c"), mixed)
    ]
    v = vr.aggregate_vision_panel(reports)
    assert v["verdict"] == "draft"
    assert v["escalated"] is True


def test_aggregate_empty_panel_defaults_to_draft():
    v = vr.aggregate_vision_panel([])
    assert v["verdict"] == "draft"
    assert v["escalated"] is True
    assert v["judge_count"] == 0


def test_aggregate_invalid_reports_stay_draft():
    bad = [vr.JudgeReport(judge="x", error="timeout")]
    v = vr.aggregate_vision_panel(bad)
    assert v["verdict"] == "draft"
    assert v["escalated"] is True


# ── entry point w/ stubbed panel ─────────────────────────────────────


def test_vision_expert_review_promotes_on_unanimous_high():
    good = {"scores": {ax: 5 for ax in vr.VISION_AXES}, "defects": [],
            "notes": "", "read_options": {"E": "temperaments"}}
    judges = [
        _judge("opus", good),
        _judge("sonnet", good),
        _judge("gemini", good),
    ]
    verdict = vr.vision_expert_review(
        GOOD_QUESTION,
        image_bytes=FAKE_IMAGE,
        media_type="image/gif",
        judges=judges,
    )
    assert verdict["verdict"] == "live"
    assert verdict["judge_count"] == 3
    assert "reviewer_notes" in verdict


def test_vision_expert_review_demotes_when_options_unreadable():
    unreadable = {
        "scores": {"correctness": 1, "clarity": 3, "distractor_quality": 1,
                   "difficulty_match": 3, "gre_authenticity": 3},
        "defects": ["image_unreadable"],
        "notes": "Options illegible in provided image",
    }
    ok = {"scores": {ax: 4 for ax in vr.VISION_AXES}, "defects": []}
    judges = [
        _judge("opus", unreadable),
        _judge("sonnet", ok),
        _judge("gemini", ok),
    ]
    verdict = vr.vision_expert_review(
        GOOD_QUESTION, image_bytes=FAKE_IMAGE, media_type="image/gif",
        judges=judges,
    )
    assert verdict["verdict"] == "draft"
    assert "image_unreadable" in verdict["defects"]


def test_vision_expert_review_survives_one_judge_network_error():
    def _boom(system, user, image_bytes, media_type):
        raise RuntimeError("503 backend unavailable")

    good = {"scores": {ax: 5 for ax in vr.VISION_AXES}}
    judges = [
        {"name": "opus", "call": _boom},
        _judge("sonnet", good),
        _judge("gemini", good),
    ]
    verdict = vr.vision_expert_review(
        GOOD_QUESTION, image_bytes=FAKE_IMAGE, media_type="image/gif",
        judges=judges,
    )
    # 2 judges succeeded with 5s — still promotes.
    assert verdict["verdict"] == "live"
    assert verdict["judge_count"] == 2
    # Failing judge surfaces in judge_notes.
    notes = verdict["judge_notes"]
    failing = [n for n in notes if n["error"]]
    assert failing and "503" in failing[0]["error"]


def test_build_user_message_has_marked_correct_label():
    msg = vr.build_vision_user_message(GOOD_QUESTION)
    assert "MARKED_CORRECT_LABEL: E" in msg
    assert "varied personalities" in msg
    assert "attached image" in msg.lower() or "options are in" in msg.lower()


def test_media_type_for_known_extensions():
    assert vr._media_type_for("x.gif") == "image/gif"
    assert vr._media_type_for("x.JPG") == "image/jpeg"
    assert vr._media_type_for("x.png") == "image/png"
