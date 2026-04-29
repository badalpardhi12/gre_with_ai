"""
Dataclasses shared across pipeline stages.

Kept dataclass-based (not Peewee models) so stages can serialize to
JSONL without touching the DB; only the persist stage hits SQLite. All
fields use only Python 3.9-friendly syntax (Optional, List, Dict — no
PEP 604 unions).
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# Pipeline stage identifiers — used as keys in the persisted JSONL files
# and as `PipelineResult.stage` for tracing failures.
class PipelineStage:
    SEED = "seed"
    GENERATE = "generate"
    SOLVE = "solve"
    AMBIGUITY = "ambiguity"
    JUDGE = "judge"
    DOMAIN = "domain"
    PERSIST = "persist"


@dataclass
class Seed:
    """One subtopic ask handed to the generator stage."""
    measure: str                    # "verbal" | "quant"
    topic: str
    subtopic: str
    subtype: str                    # tc | se | qc | mcq_single | …
    difficulty_target: int          # 1-5
    deficit: int = 0                # how many short of target_count we are
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DraftOption:
    label: str                      # "A", "B", …
    text: str
    is_correct: bool = False
    misconception: str = ""         # named distractor pattern (mcq, tc, se)


@dataclass
class DraftItem:
    """The generator's structured output, before any gates run."""
    subtype: str
    stem: str
    options: List[DraftOption]
    correct_label: str = ""         # for mcq-style; "" for numeric_entry
    explanation: str = ""
    difficulty_target: int = 3
    vocab_tier: str = "n/a"
    domain_assumptions: List[str] = field(default_factory=list)
    expected_solve_steps: int = 1
    concept_tags: List[str] = field(default_factory=list)
    stimulus: Optional[Dict[str, Any]] = None
    figure_spec: Optional[Dict[str, Any]] = None
    # Numeric entry payload (mutually exclusive with `options`):
    correct_value: Optional[float] = None
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    tolerance: Optional[float] = None
    # Trace fields populated as the pipeline progresses:
    seed: Optional[Seed] = None
    prompt_hash: str = ""
    generated_at: Optional[datetime] = None


# ── Rubric ────────────────────────────────────────────────────────────


# The 6-axis rubric — locked here so generator/judge/persist all agree on
# the keys. Behavioural-anchor wording (per band) follows the refinement
# plan §7: "5 = excellent for a high-stakes prep mock", not "5 = ETS
# clone". The anchors below are the calibration target the judge prompt
# explicitly cites; loosen them only with a corresponding calibration
# rerun.
RUBRIC_AXES = (
    "content_validity",
    "construct_alignment",
    "difficulty_plausibility",
    "distractor_quality",
    "language_clarity",
    "fairness_bias",
)

RUBRIC_AXIS_DESCRIPTIONS = {
    "content_validity": (
        "Does the item actually test the named subtopic? Would a "
        "subject-matter expert agree it belongs in a GRE bank?"
    ),
    "construct_alignment": (
        "Is the cognitive task what GRE intends (e.g., TC = inference "
        "from context cue; SE = meaning equivalence; QC = structural "
        "reasoning, not raw computation; RC = passage-bound reasoning; "
        "PS = procedural problem solving)?"
    ),
    "difficulty_plausibility": (
        "Does the item's actual difficulty plausibly match the "
        "claimed `difficulty_target` band? Score relative to the "
        "claimed band — a hard item with target=4 should score 5; a "
        "hard item with target=2 should score 2."
    ),
    "distractor_quality": (
        "Are wrong options each tied to a NAMED, plausible "
        "misconception (not filler)? Could a well-prepared but "
        "distractible test-taker be tempted by them?"
    ),
    "language_clarity": (
        "Is the stem unambiguous and readable on first pass by a "
        "non-native English GRE-prep test-taker?"
    ),
    "fairness_bias": (
        "Free of culturally specific references that disadvantage any "
        "group; no real proper nouns or post-2010 references; no "
        "offensive content."
    ),
}


# Behavioural descriptors at scores 1, 3, and 5 per axis. Rendered
# verbatim into the judge prompt so the model sees concrete language for
# the low/middle/high bands instead of inferring the scale from a
# one-line definition. Source: refinement plan §7. Keep entries short
# enough to fit comfortably inside the prompt cache.
RUBRIC_BAND_ANCHORS: Dict[str, Dict[int, str]] = {
    "content_validity": {
        5: "Tests the subtopic squarely; no other subtopic interferes.",
        3: ("Tests an adjacent subtopic, OR tests the subtopic but "
            "requires unrelated knowledge to solve."),
        1: "Tests nothing measurable; gibberish or unsolvable.",
    },
    "construct_alignment": {
        5: ("Cognitive op matches the subtype perfectly; trap "
            "structure is the canonical GRE trap for this subtype."),
        3: ("Cognitive op partially matches; the item could be solved "
            "by an off-construct shortcut."),
        1: "No identifiable cognitive op; subtype convention violated.",
    },
    "difficulty_plausibility": {
        5: "Difficulty matches the claimed target exactly.",
        3: "Difficulty 2 bands off the claimed target.",
        1: ("Item unsolvable, OR trivially solvable regardless of the "
            "claimed target."),
    },
    "distractor_quality": {
        5: ("Every distractor is named to a specific misconception a "
            "real test-taker would commit; at least one is a "
            "'second-best' answer that genuinely tempts."),
        3: "Half the distractors are named misconceptions; half are unmotivated.",
        1: ("One or more distractors is also defensibly correct, OR "
            "distractors are nonsense."),
    },
    "language_clarity": {
        5: "Crystal clear; no awkward phrasing; no extraneous info.",
        3: ("Phrasing introduces ambiguity that a careful reader can "
            "resolve."),
        1: "Stem incoherent or self-contradictory.",
    },
    "fairness_bias": {
        5: "Universally accessible; no cultural specificity.",
        3: ("Region-specific reference present; some test-takers are "
            "advantaged by familiarity with the scenario."),
        1: "Discriminatory or offensive content.",
    },
}


@dataclass
class CalibrationAnchor:
    """One worked example shipped inside every judge prompt.

    Anchors are loaded once per process from
    `services/synthetic/calibration/anchors.json` and inlined verbatim
    into the prompt. They're never scored by the judge — they're
    *reference points* so the judge sees what each band should look
    like before scoring the actual item. See refinement plan §7.
    """
    label: str                              # e.g., "GOLD-1", "BAD-2"
    description: str                        # short note, "verbal/TC, gold-anchor"
    item: Dict[str, Any]                    # the rendered item payload
    expected_scores: Dict[str, int]         # per-axis target score 1-5
    rationale: str = ""                     # optional, displayed if present

    def axis_score(self, axis: str) -> Optional[int]:
        return self.expected_scores.get(axis)


@dataclass
class JudgeAxisScore:
    axis: str
    score: int                      # 1-5
    justification: str = ""


@dataclass
class JudgeReport:
    """One judge's assessment of a draft."""
    judge_name: str                 # opaque label (e.g., "judge_a")
    item_id: str                    # caller-provided correlation id
    axes: List[JudgeAxisScore]
    raw_response: str = ""          # for debugging / audit

    def axis_score(self, axis: str) -> Optional[int]:
        for a in self.axes:
            if a.axis == axis:
                return a.score
        return None

    def mean_score(self) -> float:
        if not self.axes:
            return 0.0
        return sum(a.score for a in self.axes) / len(self.axes)

    def min_score(self) -> int:
        if not self.axes:
            return 0
        return min(a.score for a in self.axes)


@dataclass
class PipelineResult:
    """Per-item outcome at a given stage."""
    item_id: str
    stage: str                      # one of PipelineStage.*
    passed: bool
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


# ── Calibration-anchor loader ────────────────────────────────────────


# Path is overridable for tests; the default points at the in-tree
# fixture committed alongside the synthetic package.
DEFAULT_CALIBRATION_ANCHORS_PATH = (
    Path(__file__).resolve().parent / "calibration" / "anchors.json"
)


def load_calibration_anchors(
    path: Optional[Path] = None,
) -> List[CalibrationAnchor]:
    """Load anchor items from disk; raises FileNotFoundError if missing.

    The on-disk schema is intentionally simple: a list of records, each
    with `label`, `description`, `item` (a dict matching the judge
    payload schema), `expected_scores` (axis -> int 1-5), and an
    optional `rationale`. Validation is light — missing axes default to
    None and the caller may decide whether that's fatal.
    """
    p = Path(path) if path else DEFAULT_CALIBRATION_ANCHORS_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    anchors: List[CalibrationAnchor] = []
    for entry in raw:
        anchors.append(
            CalibrationAnchor(
                label=str(entry.get("label", "")),
                description=str(entry.get("description", "")),
                item=dict(entry.get("item", {})),
                expected_scores={
                    k: int(v) for k, v in (entry.get("expected_scores") or {}).items()
                },
                rationale=str(entry.get("rationale", "")),
            )
        )
    return anchors
