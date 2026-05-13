"""Tests for scripts/extract_agieval_math.py — Phase 4 · D4.

We mock:
  * dataset loading (monkeypatch ``_INJECTED_LOADER``), and
  * the LLM call (monkeypatch ``llm_service.generate_json``).

No network, no pyarrow, no HuggingFace ``datasets`` dependency needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import extract_agieval_math as eam  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────

# Three raw LSAT-LR rows in the lighteval/agi_eval_en schema.
RAW_LSAT_LR: List[Dict[str, Any]] = [
    {
        "query": "All squirrels are mammals. Some mammals hibernate. Which must be true?",
        "options": [
            "All squirrels hibernate.",
            "Some squirrels hibernate.",
            "No squirrels hibernate.",
            "Some mammals are squirrels.",
            "Most mammals are squirrels.",
        ],
        "gold": ["D"],
    },
    {
        "query": "If every student who studied passed, and Bob passed, then Bob studied. Which flaw?",
        "options": ["affirming consequent", "denying antecedent", "equivocation",
                    "circular", "hasty generalization"],
        "gold": "0",
    },
    {
        "query": "Empty gold — should be dropped.",
        "options": ["x", "y", "z", "w", "v"],
        "gold": None,
    },
]

# RC rows: first two share a passage (→ rc_multi), third has its own (→ rc_single).
RAW_LSAT_RC: List[Dict[str, Any]] = [
    {
        "passage": "Passage A about economic policy in the 19th century. " * 3,
        "query": "The primary purpose of the passage is to",
        "options": ["argue", "describe", "refute", "compare", "summarize"],
        "gold": "B",
    },
    {
        "passage": "Passage A about economic policy in the 19th century. " * 3,
        "query": "According to the author, policymakers primarily valued",
        "options": ["equity", "efficiency", "tradition", "innovation", "stability"],
        "gold": 2,
    },
    {
        "passage": "Passage B on marine biology, entirely unrelated.",
        "query": "Which best states the main idea?",
        "options": ["A", "B", "C", "D", "E"],
        "gold": "A",
    },
]

# MATH rows: two L2 items, one L4 (filtered out), one L3.
RAW_MATH: List[Dict[str, Any]] = [
    {
        "problem": "What is 7 + 5?",
        "solution": "We compute 7 + 5 = 12. $\\boxed{12}$",
        "level": "Level 2",
        "type": "Prealgebra",
    },
    {
        "problem": "Solve for x: 2x + 3 = 11.",
        "solution": "Subtract 3: 2x = 8. Divide: x = 4. $\\boxed{4}$",
        "level": "Level 2",
        "type": "Algebra",
    },
    {
        "problem": "Hard AIME problem — should be filtered.",
        "solution": "$\\boxed{42}$",
        "level": "Level 5",
        "type": "Number Theory",
    },
    {
        "problem": "How many integers between 1 and 20 are prime?",
        "solution": "Count: 2,3,5,7,11,13,17,19. $\\boxed{8}$",
        "level": "Level 3",
        "type": "Number Theory",
    },
]


def _make_loader(mapping: Dict[str, List[Dict[str, Any]]]):
    """Build a _INJECTED_LOADER stub that returns the rows for a given source."""
    def _loader(source: str) -> List[Dict[str, Any]]:
        return mapping.get(source, [])
    return _loader


@pytest.fixture
def injected_loader(monkeypatch):
    """Install a loader covering all three sources."""
    loader = _make_loader({
        "agieval_lsat_lr": RAW_LSAT_LR,
        "agieval_lsat_rc": RAW_LSAT_RC,
        "hendrycks_math": RAW_MATH,
    })
    monkeypatch.setattr(eam, "_INJECTED_LOADER", loader)
    yield loader


# ── Gold-label normalizer ─────────────────────────────────────────────

def test_gold_label_letter():
    assert eam._extract_gold_label("B") == "B"
    assert eam._extract_gold_label(["C"]) == "C"


def test_gold_label_numeric_zero_indexed():
    assert eam._extract_gold_label(0) == "A"
    assert eam._extract_gold_label(4) == "E"


def test_gold_label_numeric_one_indexed_string():
    # "5" with no 0-index collision goes to E (one-indexed path)
    assert eam._extract_gold_label("5") == "E"


def test_gold_label_none():
    assert eam._extract_gold_label(None) is None
    assert eam._extract_gold_label("") is None


# ── Normalizer: LSAT-LR ───────────────────────────────────────────────

def test_normalize_lsat_lr_tags_as_text_completion(injected_loader):
    rows = eam.load_raw_rows("agieval_lsat_lr")
    items = eam.normalize_agieval_lsat_lr(rows)
    # 3 rows raw; one was dropped (gold=None)
    assert len(items) == 2
    assert all(i.measure == "verbal" for i in items)
    assert all(i.subtype == "text_completion" for i in items)
    assert all(i.source == "agieval_lsat_lr" for i in items)
    # first row: gold=["D"]
    assert items[0].correct_answer == "D"
    # second row: gold="0" → index 0 → "A"
    assert items[1].correct_answer == "A"
    # Five options, A-E.
    assert [lbl for lbl, _ in items[0].options] == ["A", "B", "C", "D", "E"]


# ── Normalizer: LSAT-RC ───────────────────────────────────────────────

def test_normalize_lsat_rc_rc_multi_vs_rc_single(injected_loader):
    rows = eam.load_raw_rows("agieval_lsat_rc")
    items = eam.normalize_agieval_lsat_rc(rows)
    assert len(items) == 3
    # The two items that share Passage A must be rc_multi.
    multi = [i for i in items if i.passage.startswith("Passage A")]
    single = [i for i in items if i.passage.startswith("Passage B")]
    assert len(multi) == 2
    assert len(single) == 1
    assert all(i.subtype == "rc_multi" for i in multi)
    assert single[0].subtype == "rc_single"
    assert all(i.source == "agieval_lsat_rc" for i in items)
    assert all(i.measure == "verbal" for i in items)
    # gold=2 (int) → index 2 → "C"
    second = [i for i in multi if "policymakers" in i.prompt][0]
    assert second.correct_answer == "C"


# ── Normalizer: MATH ──────────────────────────────────────────────────

def test_normalize_math_filters_by_level(injected_loader):
    rows = eam.load_raw_rows("hendrycks_math")
    items = eam.normalize_hendrycks_math(rows, max_level=3)
    # Level 5 row is filtered out; 3 remain.
    assert len(items) == 3
    assert all(i.measure == "quant" for i in items)
    assert all(i.subtype == "numeric_entry" for i in items)


def test_normalize_math_source_tag_carries_level(injected_loader):
    rows = eam.load_raw_rows("hendrycks_math")
    items = eam.normalize_hendrycks_math(rows, max_level=3)
    sources = {i.source for i in items}
    assert sources == {"hendrycks_math_L2", "hendrycks_math_L3"}


def test_normalize_math_extracts_boxed_answer(injected_loader):
    rows = eam.load_raw_rows("hendrycks_math")
    items = eam.normalize_hendrycks_math(rows, max_level=3)
    answers = sorted(i.correct_answer for i in items)
    assert answers == ["12", "4", "8"]


# ── Run: reformat off ─────────────────────────────────────────────────

def test_run_math_no_reformat_dry_run(injected_loader):
    summary = eam.run(source="math", dry_run=True, reformat=False)
    assert summary["items_normalized"] == 3
    assert summary["llm_calls"] == 0
    assert summary["inserted"] == 0
    assert summary["dry_run"] is True


def test_run_agieval_meta_source_union(injected_loader):
    summary = eam.run(source="agieval", dry_run=True, reformat=False)
    # 2 LR + 3 RC = 5
    assert summary["items_normalized"] == 5
    by_subtype = summary["by_subtype"]
    assert by_subtype["text_completion"] == 2
    assert by_subtype["rc_multi"] == 2
    assert by_subtype["rc_single"] == 1


def test_run_respects_max_items(injected_loader):
    summary = eam.run(source="agieval", max_items=3, dry_run=True, reformat=False)
    assert summary["items_normalized"] == 3


# ── Run: reformat on (LLM mocked) ─────────────────────────────────────

class _FakeLLM:
    """Deterministic stand-in for services.llm_service.llm_service."""

    def __init__(self):
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt, **_kwargs):
        self.calls += 1
        # Always emit a well-formed 5-option MCQ.
        return {
            "prompt": f"[GRE-reformatted #{self.calls}] {user_prompt.splitlines()[0][:60]}",
            "options": [
                {"label": "A", "text": "option alpha"},
                {"label": "B", "text": "option beta"},
                {"label": "C", "text": "option gamma"},
                {"label": "D", "text": "option delta"},
                {"label": "E", "text": "option epsilon"},
            ],
            "correct_answer": "B",
            "explanation": "Because B is the correct letter in the fake LLM.",
        }


def test_run_reformat_replaces_prompt_and_options(injected_loader, monkeypatch):
    fake = _FakeLLM()
    # Patch the llm_service singleton used inside run().
    monkeypatch.setattr("services.llm_service.llm_service", fake)

    summary = eam.run(source="math", dry_run=True, reformat=True)
    assert summary["items_normalized"] == 3
    assert summary["llm_calls"] == 3
    assert fake.calls == 3
    # Reformatted subtype for quant flips to mcq_single when options appear.
    assert summary["by_subtype"]["mcq_single"] == 3


def test_run_reformat_drops_items_llm_fails(injected_loader, monkeypatch):
    """LLM errors must not crash the run; failing items are silently dropped."""
    class _FailingLLM:
        def generate_json(self, *_a, **_kw):
            raise RuntimeError("simulated LLM timeout")

    monkeypatch.setattr("services.llm_service.llm_service", _FailingLLM())

    summary = eam.run(source="math", dry_run=True, reformat=True)
    assert summary["llm_calls"] == 3
    assert summary["items_normalized"] == 0


# ── Subtype classification, per source ────────────────────────────────

def test_subtype_classification_table(injected_loader):
    """Explicit end-to-end mapping from source → expected subtypes."""
    for src, expected in [
        ("agieval_lsat_lr", {"text_completion"}),
        ("agieval_lsat_rc", {"rc_single", "rc_multi"}),
        ("math", {"numeric_entry"}),
    ]:
        summary = eam.run(source=src, dry_run=True, reformat=False)
        assert set(summary["by_subtype"].keys()) <= expected, (
            f"{src}: got subtypes {summary['by_subtype']}, expected {expected}"
        )


# ── DB insertion + idempotency ────────────────────────────────────────

def test_db_insertion_math_as_numeric_entry(temp_db, injected_loader):
    from models.database import Question, NumericAnswer

    summary = eam.run(source="math", reformat=False, dry_run=False)
    assert summary["inserted"] == 3
    assert summary["skipped"] == 0

    rows = list(Question.select()
                .where(Question.source.startswith("hendrycks_math_"))
                .order_by(Question.source_anchor))
    assert len(rows) == 3
    assert all(r.status == "candidate" for r in rows)
    assert all(r.subtype == "numeric_entry" for r in rows)
    assert all(r.measure == "quant" for r in rows)

    # Each should have exactly one NumericAnswer with a real value.
    for q in rows:
        na = list(q.numeric_answers)
        assert len(na) == 1
        assert na[0].exact_value is not None


def test_db_insertion_lsat_lr_with_options(temp_db, injected_loader):
    from models.database import Question

    summary = eam.run(source="agieval_lsat_lr", reformat=False, dry_run=False)
    assert summary["inserted"] == 2
    rows = list(Question.select()
                .where(Question.source == "agieval_lsat_lr"))
    first = rows[0]
    assert first.subtype == "text_completion"
    assert first.status == "candidate"
    labels = sorted(o.option_label for o in first.options)
    assert labels == ["A", "B", "C", "D", "E"]
    correct = [o for o in first.options if o.is_correct]
    assert len(correct) == 1


def test_db_insertion_is_idempotent(temp_db, injected_loader):
    s1 = eam.run(source="math", reformat=False, dry_run=False)
    s2 = eam.run(source="math", reformat=False, dry_run=False)
    assert s1["inserted"] == 3
    assert s2["inserted"] == 0
    assert s2["skipped"] == 3


def test_db_insertion_with_reformat(temp_db, injected_loader, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr("services.llm_service.llm_service", fake)
    summary = eam.run(source="math", reformat=True, dry_run=False)
    assert summary["inserted"] == 3
    from models.database import Question
    rows = list(Question.select()
                .where(Question.source.startswith("hendrycks_math_")))
    # After reformat, options are present and subtype flipped to mcq_single.
    assert all(r.subtype == "mcq_single" for r in rows)
    for r in rows:
        assert len(list(r.options)) == 5
        correct = [o for o in r.options if o.is_correct]
        assert len(correct) == 1 and correct[0].option_label == "B"
    # Provenance reflects reformat.
    sample = rows[0]
    assert sample.provenance == "llm_reviewed"


# ── CLI smoke ─────────────────────────────────────────────────────────

def test_cli_dry_run_exits_zero(injected_loader, capsys):
    rc = eam.main(["--source", "math", "--max-items", "2", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["items_normalized"] == 2
    assert parsed["dry_run"] is True
