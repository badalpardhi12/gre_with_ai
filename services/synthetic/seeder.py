"""
Seeder — stage (a).

Two interfaces ship in this module:

- `build_seeds(n)`: the original deficit-weighted picker. Kept for
  back-compat with the calibration script and ad-hoc smoke runs.
- `DiversitySampler`: stratified, history-aware sampler from refinement
  plan §6. Emits seeds covering five dimensions (subtopic × difficulty
  × scenario_class × persona × structural_frame) with Latin-Hypercube
  -style coverage and a rolling-window dedup against the last K runs.

Both compute per-subtopic deficits from the taxonomy + live Question
table; the sampler additionally tracks scenario / persona / frame
distributions per subtopic so back-to-back seeds don't collide.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from models.database import Question
from models.taxonomy import (
    QUANT_TAXONOMY, VERBAL_TAXONOMY, get_subtopic_meta,
)
from services.log import get_logger
from services.question_bank import SYNTHETIC_SOURCE
from services.synthetic.types import Seed

logger = get_logger("synthetic.seeder")


# Default mix per the plan §4.
DEFAULT_DIFFICULTY_DISTRIBUTION = {2: 0.30, 3: 0.45, 4: 0.25}


# Subtopic -> primary subtype. Quant subtopics with multiple subtypes
# (mcq_single + qc + numeric_entry) get a small weighted mix.
SUBTOPIC_SUBTYPE_MIX: Dict[str, Dict[str, float]] = {
    # Verbal — one subtype each
    "tc_1_blank": {"tc": 1.0},
    "tc_2_blank": {"tc": 1.0},
    "tc_3_blank": {"tc": 1.0},
    "se_synonyms": {"se": 1.0},
    "se_contrast": {"se": 1.0},
    "rc_main_idea": {"rc_single": 1.0},
    "rc_detail": {"rc_single": 1.0},
    "rc_inference": {"rc_single": 1.0},
    "rc_tone_attitude": {"rc_single": 1.0},
    "rc_structure_function": {"rc_single": 1.0},
    "rc_vocab_in_context": {"rc_single": 1.0},
    "rc_select_sentence": {"rc_select_passage": 1.0},
    "rc_multi_answer": {"rc_multi": 1.0},
    "cr_assumption": {"rc_single": 1.0},
    "cr_strengthen": {"rc_single": 1.0},
    "cr_weaken": {"rc_single": 1.0},
    "cr_evaluate": {"rc_single": 1.0},
    # Quant — most subtopics support both PS and QC
    "data_interpretation": {"data_interp": 1.0},
}

DEFAULT_QUANT_SUBTYPE_MIX = {
    "mcq_single": 0.55, "qc": 0.30, "numeric_entry": 0.15,
}


def get_subtype_mix(subtopic: str) -> Dict[str, float]:
    if subtopic in SUBTOPIC_SUBTYPE_MIX:
        return SUBTOPIC_SUBTYPE_MIX[subtopic]
    return dict(DEFAULT_QUANT_SUBTYPE_MIX)


def live_count_per_subtopic(include_synthetic: bool = True) -> Dict[str, int]:
    """Count live items grouped by subtopic. Useful for deficit math."""
    query = Question.select().where(Question.status == "live")
    if not include_synthetic:
        query = query.where(Question.source != SYNTHETIC_SOURCE)
    counts: Dict[str, int] = {}
    for q in query:
        if not q.subtopic:
            continue
        counts[q.subtopic] = counts.get(q.subtopic, 0) + 1
    return counts


def compute_deficits(
    *,
    include_synthetic_in_count: bool = True,
) -> List[Dict[str, object]]:
    """Return [{measure, topic, subtopic, target, live, deficit, weight}]."""
    live = live_count_per_subtopic(include_synthetic=include_synthetic_in_count)
    rows: List[Dict[str, object]] = []
    for measure, taxonomy in (("quant", QUANT_TAXONOMY),
                              ("verbal", VERBAL_TAXONOMY)):
        for topic, td in taxonomy.items():
            for sub, sd in td["subtopics"].items():
                target = sd.get("target_count", 0)
                count = live.get(sub, 0)
                deficit = max(0, target - count)
                rows.append({
                    "measure": measure,
                    "topic": topic,
                    "subtopic": sub,
                    "target": target,
                    "live": count,
                    "deficit": deficit,
                    "frequency_weight": sd.get("frequency_weight", 1.0),
                })
    return rows


def _pick_difficulty(rng: random.Random,
                     dist: Dict[int, float] = DEFAULT_DIFFICULTY_DISTRIBUTION) -> int:
    bands = list(dist.keys())
    weights = list(dist.values())
    return rng.choices(bands, weights=weights, k=1)[0]


def _pick_subtype(rng: random.Random, mix: Dict[str, float]) -> str:
    items = list(mix.keys())
    weights = [mix[i] for i in items]
    return rng.choices(items, weights=weights, k=1)[0]


def build_seeds(
    n: int,
    *,
    rng: Optional[random.Random] = None,
    measure_filter: Optional[str] = None,
    subtopic_filter: Optional[Iterable[str]] = None,
    difficulty_distribution: Optional[Dict[int, float]] = None,
) -> List[Seed]:
    """Build N seeds weighted by deficit × frequency_weight.

    `rng` injectable for deterministic tests.
    `subtopic_filter` restricts the pool — handy for ad-hoc batch runs.
    """
    rng = rng or random.Random()
    rows = compute_deficits()
    if measure_filter:
        rows = [r for r in rows if r["measure"] == measure_filter]
    if subtopic_filter:
        wanted = set(subtopic_filter)
        rows = [r for r in rows if r["subtopic"] in wanted]
    # Weight by deficit × freq; rows with deficit=0 still get a tiny
    # weight so the picker isn't NaN when all caught up.
    weighted = []
    for r in rows:
        w = max(1.0, float(r["deficit"])) * float(r["frequency_weight"])
        weighted.append((r, w))
    if not weighted:
        return []
    rows_only = [w[0] for w in weighted]
    weights = [w[1] for w in weighted]
    seeds: List[Seed] = []
    dist = difficulty_distribution or DEFAULT_DIFFICULTY_DISTRIBUTION
    for _ in range(n):
        choice = rng.choices(rows_only, weights=weights, k=1)[0]
        difficulty = _pick_difficulty(rng, dist)
        subtype_mix = get_subtype_mix(str(choice["subtopic"]))
        subtype = _pick_subtype(rng, subtype_mix)
        seeds.append(Seed(
            measure=str(choice["measure"]),
            topic=str(choice["topic"]),
            subtopic=str(choice["subtopic"]),
            subtype=subtype,
            difficulty_target=difficulty,
            deficit=int(choice["deficit"]),
        ))
    return seeds


# ── DiversitySampler ──────────────────────────────────────────────────


# Persona / scenario registries. Plan §6: persona shapes *how* a
# passage or problem is framed (academic vs journalistic vs
# scientific-textbook vs policy-brief). Scenario_class is *what* the
# passage or problem is about (humanities vs biosci vs everyday).
# Both are sampled per-seed so back-to-back items don't collide.
DEFAULT_VERBAL_SCENARIOS = (
    "humanities", "biological_sciences", "physical_sciences",
    "social_sciences", "everyday",
)
DEFAULT_QUANT_SCENARIOS = (
    "abstract", "ratio_word", "geometry_concrete",
    "real_world_data", "lab_experiment", "agriculture",
    "manufacturing", "weather_climate_neutral",
)
DEFAULT_VERBAL_PERSONAS = (
    "academic_neutral", "journalistic", "scientific_textbook",
    "policy_brief", "historical_essay",
)
DEFAULT_QUANT_PERSONAS = (
    "lab_experiment", "manufacturing", "agriculture",
    "population_demographics", "weather_climate_neutral",
    "abstract_word",
)


# Subtype-specific structural frames. For TC, the frame is the cue
# class (contrast / continuation / etc.); for QC, it's the trap class
# (assumes_positive / assumes_integer / ...). For other subtypes a
# single "default" frame is used (sampler still rotates persona +
# scenario, just not the frame).
STRUCTURAL_FRAMES_BY_SUBTYPE: Dict[str, Tuple[str, ...]] = {
    "tc": ("contrast", "continuation", "causal", "concession", "qualification"),
    "se": ("synonyms", "contrast"),
    "qc": (
        "assumes_positive", "assumes_integer", "assumes_nonzero",
        "assumes_distinct", "ignores_constraint",
    ),
    "mcq_single": (
        "off_by_one", "swapped_quantities", "applied_wrong_operation",
        "ignored_unit_conversion", "computed_intermediate_as_final",
    ),
    "mcq_multi": ("one_correct_too_few", "claims_implication_not_present"),
    "numeric_entry": ("integer_answer", "decimal_answer", "fraction_answer"),
    "rc_single": (
        "main_idea", "detail", "inference", "tone_attitude",
        "structure_function", "vocab_in_context",
    ),
    "rc_multi": ("inference", "detail"),
    "data_interp": ("read_a_value", "compute_derived"),
}


def _scenarios_for(measure: str) -> Tuple[str, ...]:
    return DEFAULT_QUANT_SCENARIOS if measure == "quant" else DEFAULT_VERBAL_SCENARIOS


def _personas_for(measure: str) -> Tuple[str, ...]:
    return DEFAULT_QUANT_PERSONAS if measure == "quant" else DEFAULT_VERBAL_PERSONAS


def _frames_for(subtype: str) -> Tuple[str, ...]:
    return STRUCTURAL_FRAMES_BY_SUBTYPE.get(subtype, ("default",))


@dataclass
class _RecentBatchTracker:
    """Per-subtopic deque of recent (scenario, persona, frame, difficulty)
    tuples; rejects re-use within `window` seeds."""
    window: int = 200
    _by_subtopic: Dict[str, Deque[Tuple]] = field(default_factory=dict, init=False)

    def seen(self, subtopic: str, key: Tuple) -> bool:
        return key in self._by_subtopic.get(subtopic, deque())

    def record(self, subtopic: str, key: Tuple) -> None:
        bucket = self._by_subtopic.setdefault(subtopic, deque(maxlen=self.window))
        bucket.append(key)

    def reset(self) -> None:
        self._by_subtopic.clear()


@dataclass
class DiversitySampler:
    """Stratified, history-aware seed sampler (plan §6).

    The `sample` method draws N seeds with Latin-Hypercube-style
    coverage of the five dimensions. A rolling window of recent
    (scenario, persona, frame, difficulty) tuples per subtopic is
    used to reject same-key collisions; if no fresh tuple is
    available after `max_attempts` the sampler relaxes the persona
    constraint (plan §11 risk #3 mitigation).

    Determinism: pass an `rng` for reproducibility. The sampler emits
    the same seeds for the same RNG state, modulo the recent-batch
    deque (which is per-instance).
    """
    difficulty_distribution: Dict[int, float] = field(
        default_factory=lambda: dict(DEFAULT_DIFFICULTY_DISTRIBUTION)
    )
    window: int = 200
    max_attempts: int = 6
    _tracker: _RecentBatchTracker = field(init=False)

    def __post_init__(self):
        self._tracker = _RecentBatchTracker(window=self.window)

    def reset_history(self) -> None:
        self._tracker.reset()

    def sample(
        self,
        n: int,
        *,
        rng: Optional[random.Random] = None,
        measure_filter: Optional[str] = None,
        subtopic_filter: Optional[Iterable[str]] = None,
    ) -> List[Seed]:
        rng = rng or random.Random()
        rows = compute_deficits()
        if measure_filter:
            rows = [r for r in rows if r["measure"] == measure_filter]
        if subtopic_filter:
            wanted = set(subtopic_filter)
            rows = [r for r in rows if r["subtopic"] in wanted]
        if not rows:
            return []

        # Weight by deficit × frequency (matches build_seeds). Rows
        # with deficit==0 still get a tiny weight so the sampler
        # doesn't NaN when caught up.
        weights: List[float] = []
        for r in rows:
            w = max(1.0, float(r["deficit"])) * float(r["frequency_weight"])
            weights.append(w)

        # Latin-Hypercube-style: pre-shuffle the difficulty levels
        # across the N seeds so each band appears in proportion to
        # its target distribution.
        diff_pool = self._latin_hypercube(n, self.difficulty_distribution, rng)
        seeds: List[Seed] = []
        for i in range(n):
            row = rng.choices(rows, weights=weights, k=1)[0]
            difficulty = diff_pool[i]
            subtype_mix = get_subtype_mix(str(row["subtopic"]))
            subtype = _pick_subtype(rng, subtype_mix)
            measure = str(row["measure"])
            scenario_pool = _scenarios_for(measure)
            persona_pool = _personas_for(measure)
            frame_pool = _frames_for(subtype)

            scenario, persona, frame = self._pick_dimensions(
                rng,
                subtopic=str(row["subtopic"]),
                difficulty=difficulty,
                scenario_pool=scenario_pool,
                persona_pool=persona_pool,
                frame_pool=frame_pool,
            )
            seeds.append(Seed(
                measure=measure,
                topic=str(row["topic"]),
                subtopic=str(row["subtopic"]),
                subtype=subtype,
                difficulty_target=difficulty,
                deficit=int(row["deficit"]),
                extra={
                    "scenario_class": scenario,
                    "persona": persona,
                    "structural_frame": frame,
                },
            ))
        return seeds

    @staticmethod
    def _latin_hypercube(
        n: int,
        distribution: Dict[int, float],
        rng: random.Random,
    ) -> List[int]:
        """Build N difficulty values whose empirical distribution
        matches `distribution` as closely as possible, then shuffle.

        This is a coarse 1-D Latin-Hypercube: for each difficulty band
        we add ceil(n * weight) seeds, then trim/shuffle to length n.
        Beats independent sampling because it eliminates run-to-run
        variance in the difficulty mix.
        """
        if not distribution:
            return [3] * n
        total_w = sum(distribution.values())
        pool: List[int] = []
        for band, w in distribution.items():
            pool.extend([band] * max(1, int(round(n * w / total_w))))
        # Pad / trim to length n.
        while len(pool) < n:
            pool.append(rng.choices(list(distribution.keys()),
                                    weights=list(distribution.values()), k=1)[0])
        rng.shuffle(pool)
        return pool[:n]

    def _pick_dimensions(
        self,
        rng: random.Random,
        *,
        subtopic: str,
        difficulty: int,
        scenario_pool: Tuple[str, ...],
        persona_pool: Tuple[str, ...],
        frame_pool: Tuple[str, ...],
    ) -> Tuple[str, str, str]:
        """Pick (scenario, persona, frame) avoiding recent collisions.

        After `max_attempts` failed picks, relax the persona constraint
        (per plan §11 risk #3). In the final fallback we still record
        the chosen tuple in the history so repeats are tracked.
        """
        for _ in range(self.max_attempts):
            scenario = rng.choice(scenario_pool)
            persona = rng.choice(persona_pool)
            frame = rng.choice(frame_pool)
            key = (scenario, persona, frame, difficulty)
            if not self._tracker.seen(subtopic, key):
                self._tracker.record(subtopic, key)
                return scenario, persona, frame
        # Relaxed: drop the persona constraint, accept duplicate persona.
        scenario = rng.choice(scenario_pool)
        persona = rng.choice(persona_pool)
        frame = rng.choice(frame_pool)
        key = (scenario, persona, frame, difficulty)
        self._tracker.record(subtopic, key)
        return scenario, persona, frame
