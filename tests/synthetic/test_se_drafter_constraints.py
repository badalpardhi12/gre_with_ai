"""
SE drafter constraints + solver-reconcile regression tests.

Covers the phase-1 prod failure mode: drafter labels pair X,Y as correct
but both cold solvers agree on pair Y,Z — the true contextual synonym
pair sits among the distractors.

We want the pipeline to:
  (a) recognise the pattern (`reconcile_se_key`),
  (b) swap the draft's `correct_label` and `is_correct` flags to the
      solver-agreed pair (`apply_se_key_swap`),
  (c) let the item persist instead of rejecting outright.

These tests also assert the reconciler refuses to swap when the signal
is ambiguous (solvers disagree with each other, output is malformed,
agreed pair would be the same as the key, or letters are off-grid).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from services.synthetic.llm_client import LLMClient, LLMResponse
from services.synthetic.solver import (
    AdversarialSolver, SolveAttempt, apply_se_key_swap, reconcile_se_key,
)
from services.synthetic.types import DraftItem, DraftOption, Seed


def _se_payload(claimed: str = "A,C") -> Dict[str, Any]:
    """Payload for an SE item where the drafter labels A,C as correct
    but the real synonym pair (in this contrived fixture) is B,D."""
    options = [
        {"label": "A", "text": "sprawling", "is_correct": False,
         "misconception": "grammatical_fit_wrong_meaning"},
        {"label": "B", "text": "meticulous", "is_correct": False,
         "misconception": ""},
        {"label": "C", "text": "flamboyant", "is_correct": False,
         "misconception": "grammatical_fit_wrong_meaning"},
        {"label": "D", "text": "fastidious", "is_correct": False,
         "misconception": ""},
        {"label": "E", "text": "lethargic", "is_correct": False,
         "misconception": "narrower_scope"},
        {"label": "F", "text": "verbose", "is_correct": False,
         "misconception": "register_mismatch"},
    ]
    # Mark claimed-correct options
    winners = {p.strip().upper() for p in claimed.split(",") if p.strip()}
    for o in options:
        if o["label"] in winners:
            o["is_correct"] = True
    return {
        "subtype": "se",
        "stem": ("The editor insisted that every footnote be "
                 "______, refusing to sign off on the manuscript "
                 "until each citation had been double-checked."),
        "options": options,
        "correct_label": claimed,
        "explanation": "The context demands a synonym for 'careful'.",
        "difficulty_target": 3,
        "vocab_tier": "advanced",
        "domain_assumptions": [],
        "expected_solve_steps": 1,
        "concept_tags": [],
    }


def _attempt(name: str, chose: str, matches: bool = False) -> SolveAttempt:
    return SolveAttempt(
        solver_name=name,
        chosen=chose,
        matches_key=matches,
        reasoning_trace_chars=120,
        raw_response=f"reasoning...\nANSWER: {chose}",
    )


# ── reconcile_se_key ────────────────────────────────────────────────


def test_reconcile_swaps_when_solvers_agree_on_other_pair():
    """Both solvers agree on B,D; drafter labeled A,C. Swap."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "B,D"),
        _attempt("solver_b", "B,D"),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is True
    assert res.new_label == "B,D"
    assert "B,D" in res.solver_agreed_pair


def test_reconcile_canonicalises_solver_pair_order():
    """Solver may report 'D,B'; reconciler normalises to 'B,D'."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "D,B"),
        _attempt("solver_b", "B,D"),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is True
    assert res.new_label == "B,D"


def test_reconcile_refuses_when_solvers_disagree():
    """Solver_a picks B,D; solver_b picks A,E. No cold agreement → no swap."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "B,D"),
        _attempt("solver_b", "A,E"),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is False
    assert "disagree" in res.reason.lower()


def test_reconcile_refuses_when_agreed_matches_key():
    """Solvers agree with the drafter — nothing to swap."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "A,C", matches=True),
        _attempt("solver_b", "A,C", matches=True),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is False
    assert "match" in res.reason.lower()


def test_reconcile_refuses_on_malformed_solver_output():
    """Solver produced a single letter, or nothing. Don't swap."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "B"),
        _attempt("solver_b", "B,D"),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is False


def test_reconcile_refuses_when_agreed_letters_out_of_grid():
    """Solvers returned G,H — outside the SE A-F letter range."""
    payload = _se_payload(claimed="A,C")
    attempts = [
        _attempt("solver_a", "G,H"),
        _attempt("solver_b", "G,H"),
    ]
    res = reconcile_se_key(payload, "A,C", attempts)
    assert res.should_swap is False


def test_reconcile_noop_for_non_se():
    """Reconciler is SE-only; MCQ passes through untouched."""
    payload = {"subtype": "mcq_single", "options": [
        {"label": l, "text": l, "is_correct": l == "C"}
        for l in "ABCDE"
    ], "correct_label": "C"}
    attempts = [
        _attempt("solver_a", "D"),
        _attempt("solver_b", "D"),
    ]
    res = reconcile_se_key(payload, "C", attempts)
    assert res.should_swap is False


# ── apply_se_key_swap ───────────────────────────────────────────────


def test_apply_swap_updates_draft_flags():
    """Mutating the DraftItem flips is_correct on the winning pair and
    clears it on the old pair."""
    payload = _se_payload(claimed="A,C")
    options = [DraftOption(**o) for o in payload["options"]]
    draft = DraftItem(
        subtype="se", stem=payload["stem"], options=options,
        correct_label="A,C", explanation=payload["explanation"],
        difficulty_target=3,
        seed=Seed(measure="verbal", topic="sentence_equivalence",
                   subtopic="se_synonyms", subtype="se",
                   difficulty_target=3),
    )
    apply_se_key_swap(draft, "B,D")
    assert draft.correct_label == "B,D"
    winners = [o.label for o in draft.options if o.is_correct]
    losers = [o.label for o in draft.options if not o.is_correct]
    assert sorted(winners) == ["B", "D"]
    assert "A" in losers and "C" in losers


def test_apply_swap_handles_dict_payload():
    """apply_se_key_swap also accepts a dict-shaped payload for tests
    that don't construct a full DraftItem."""
    payload = _se_payload(claimed="A,C")
    apply_se_key_swap(payload, "B,D")
    assert payload["correct_label"] == "B,D"
    winners = [o["label"] for o in payload["options"] if o["is_correct"]]
    assert sorted(winners) == ["B", "D"]


def test_apply_swap_empty_label_is_noop():
    payload = _se_payload(claimed="A,C")
    original = json.dumps(payload, sort_keys=True)
    apply_se_key_swap(payload, "")
    assert json.dumps(payload, sort_keys=True) == original


# ── End-to-end pipeline: mislabelled SE gets salvaged ───────────────


class _SolverClient(LLMClient):
    """Canned solver that returns a fixed letter/pair."""
    def __init__(self, choice: str, model_alias: str = "stub"):
        self.choice = choice
        self.model_alias = model_alias

    def complete(self, *a, **kw):
        return LLMResponse(text=f"reasoning...\nANSWER: {self.choice}")

    def complete_json(self, *a, **kw):
        raise NotImplementedError


def test_solver_reconcile_end_to_end_salvages_mislabelled_se():
    """AdversarialSolver plus reconcile_se_key plus apply_se_key_swap
    turns a rejectable disagreement into a persistable item.

    This is the exact scenario that sank 34 of 40 SE seeds in the
    phase-1 prod run.
    """
    payload = _se_payload(claimed="A,C")
    solver = AdversarialSolver({
        "solver_a": _SolverClient("B,D"),
        "solver_b": _SolverClient("B,D"),
    })
    attempts = solver.attempt(payload, payload["correct_label"])
    gate_before = solver.gate("test-se-salvage", attempts)
    assert gate_before.passed is False, "gate should fail before reconcile"

    reconcile = reconcile_se_key(payload, payload["correct_label"], attempts)
    assert reconcile.should_swap is True
    assert reconcile.new_label == "B,D"

    apply_se_key_swap(payload, reconcile.new_label)
    # Recompute matches_key with the new authoritative label.
    from services.synthetic.solver import answers_match
    for a in attempts:
        a.matches_key = answers_match(reconcile.new_label, a.chosen)
    gate_after = solver.gate("test-se-salvage", attempts)
    assert gate_after.passed is True
    assert payload["correct_label"] == "B,D"
