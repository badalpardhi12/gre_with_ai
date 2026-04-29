"""Tests for ``scripts.dedup_cross_bank`` — fingerprint + cluster logic.

These don't touch the real DB or sentence-transformers; they just lock
in the deterministic pieces (normalisation, keeper selection,
union-find clustering) so regressions are caught cheaply.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dedup_cross_bank import (  # noqa: E402
    SOURCE_PRIORITY,
    build_pair_keeper_plan,
    fingerprint,
    pair_key,
)


class _Row:
    """Minimal stand-in for a Peewee row."""
    def __init__(self, qid: int, source: str, prompt: str = ""):
        self.id = qid
        self.source = source
        self.prompt = prompt


def test_fingerprint_normalizes_whitespace_and_punct():
    a = "What is 2 + 2? The answer."
    b = "what   is 2+2 the answer"
    c = "WHAT IS 2+2 THE ANSWER!"
    assert fingerprint(a) == fingerprint(b) == fingerprint(c)


def test_fingerprint_differs_for_different_content():
    assert fingerprint("abc") != fingerprint("def")


def test_fingerprint_empty_string_is_empty():
    assert fingerprint("") == ""
    assert fingerprint(None or "") == ""


def test_pair_key_canonicalizes_order():
    assert pair_key(5, 3) == (3, 5)
    assert pair_key(3, 5) == (3, 5)


def test_source_priority_princeton_outranks_kaplan():
    assert SOURCE_PRIORITY["princeton_2012"] > SOURCE_PRIORITY["kaplan_2024"]
    assert SOURCE_PRIORITY["kaplan_2024"] > SOURCE_PRIORITY["manhattan_5lb_2018"]
    assert SOURCE_PRIORITY["manhattan_5lb_2018"] > SOURCE_PRIORITY["ai_generated"]
    assert SOURCE_PRIORITY["ai_generated"] > SOURCE_PRIORITY["ai_synthetic"]


def test_pair_plan_keeps_higher_priority_source():
    id_to_row = {
        1: _Row(1, "ai_generated"),
        2: _Row(2, "princeton_2012"),  # wins
    }
    pairs = [(1, 2, 0.99)]
    plan, loser_to_keeper = build_pair_keeper_plan(pairs, id_to_row)
    assert plan == [(1, 2, 0.99)]
    assert loser_to_keeper == {1: 2}


def test_pair_plan_tiebreaks_by_lower_qid():
    id_to_row = {
        5: _Row(5, "princeton_2012"),
        3: _Row(3, "princeton_2012"),  # lower id wins the tie
    }
    pairs = [(3, 5, 0.99)]
    plan, loser_to_keeper = build_pair_keeper_plan(pairs, id_to_row)
    assert loser_to_keeper == {5: 3}


def test_pair_plan_prefers_best_keeper_across_multiple_pairs():
    # Loser=1, two candidate keepers. Princeton should win over kaplan.
    id_to_row = {
        1: _Row(1, "ai_generated"),
        2: _Row(2, "kaplan_2024"),
        3: _Row(3, "princeton_2012"),
    }
    pairs = [(1, 2, 0.96), (1, 3, 0.95)]
    plan, loser_to_keeper = build_pair_keeper_plan(pairs, id_to_row)
    assert loser_to_keeper[1] == 3


def test_pair_plan_no_pairs_yields_empty_plan():
    plan, loser_to_keeper = build_pair_keeper_plan([], {})
    assert plan == []
    assert loser_to_keeper == {}
