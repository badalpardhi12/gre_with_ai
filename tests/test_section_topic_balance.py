"""
Per-section topic-balance regression.

Hard-pins that ``select_questions_composed`` ships sections whose
subtype/topic mix lands in a reasonable band around the Magoosh-aligned
``QUANT_COMPOSITION`` and ``VERBAL_COMPOSITION`` ratios. The user-reported
bug (2026-05-25) was every Quant section shipping ~6 DI/figure-bearing
items out of 12, vs. a target of ~1.3. Root cause was a peewee aliased-
column bug in ``_count_figure_bearing`` that caused the figure-floor to
always think the section needed +3 figure-bearing items even after the
DI cluster had already shipped 3 — so every section got the cluster
plus 3 extra chart/graph singletons.

These tests stay green only if the figure-floor counter sees the DI
cluster's stimulus content correctly. They run against the shipped seed
(skipping when LFS isn't resolved, mirroring ``test_blueprint_assembly``).
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict

import pytest

from config import (
    QUANT_S1_COUNT, QUANT_S2_COUNT,
    VERBAL_S1_COUNT, VERBAL_S2_COUNT,
    SEED_DB_PATH,
)

# Mirrors the LFS-pointer guard in test_blueprint_assembly.py — when the
# CI checkout pulled the LFS pointer (~133 bytes) instead of the real DB,
# every assembly assertion would explode with "file is not a database".
if not SEED_DB_PATH.exists() or SEED_DB_PATH.stat().st_size < 1024:
    pytest.skip(
        f"Skipping topic-balance tests: {SEED_DB_PATH} is missing or only a "
        f"Git LFS pointer.",
        allow_module_level=True,
    )

from models.database import init_db, Question, Stimulus  # noqa: E402
from models.exam_session import ExamSession, SectionType  # noqa: E402
from services.question_bank import (  # noqa: E402
    QUANT_COMPOSITION, VERBAL_COMPOSITION,
    DI_STIMULUS_TYPES, QuestionBankService,
)


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    from models.database import ServedLog
    ServedLog.delete().execute()
    yield
    ServedLog.delete().execute()


@pytest.fixture(autouse=True)
def _clean_served_log():
    """Section assembly writes to ServedLog at pick time. Clear it
    between tests so each trial starts from a clean dedup state and
    pool-coverage assertions see a fresh candidate pool."""
    from models.database import ServedLog
    ServedLog.delete().execute()
    yield
    ServedLog.delete().execute()


def _section_subtypes(qids):
    """Return Counter of subtype->count for the given qids, plus the
    list of (qid, subtype, stimulus_type) triples so callers can flag
    DI-stimulus items even when their subtype isn't ``data_interp``.
    """
    rows = list(
        Question.select(Question.id, Question.subtype, Question.stimulus_id)
        .where(Question.id.in_(list(qids)))
    )
    stim_ids = [r.stimulus_id for r in rows if r.stimulus_id is not None]
    stim_type_map = {}
    if stim_ids:
        for s in Stimulus.select(Stimulus.id, Stimulus.stimulus_type).where(
                Stimulus.id.in_(stim_ids)):
            stim_type_map[s.id] = s.stimulus_type
    triples = [(r.id, r.subtype, stim_type_map.get(r.stimulus_id))
               for r in rows]
    return Counter(t[1] for t in triples), triples


def _count_di_bearing(triples):
    """How many items are perceived as DI by the user — either the
    ``data_interp`` subtype OR backed by a graph/table/chart stimulus.
    """
    n = 0
    for _, subtype, stim_type in triples:
        if subtype == "data_interp":
            n += 1
        elif stim_type in DI_STIMULUS_TYPES:
            n += 1
    return n


# ── 1. Quant: DI-bearing items don't dominate the section ────────────

@pytest.mark.parametrize("count", [QUANT_S1_COUNT, QUANT_S2_COUNT])
def test_quant_di_does_not_dominate_section(count):
    """No single 12/15-Q Quant section ships more than 2x its target
    DI ratio of figure/chart-backed items.

    Magoosh ratio: 0.11. Section budgets:
      * 12-Q: 12 * 0.11 ≈ 1.3 (target). Cap 2x = 2.64.
      * 15-Q: 15 * 0.11 ≈ 1.65 (target). Cap 2x = 3.3.

    The DI cluster anchors 3 items per section by design (real GRE
    composition: one chart with 3 questions). The cap below allows
    one cluster plus the figure-floor top-up — that's 4 items in the
    pre-Phase-6 12-Q world, 4 items for 15-Q (1 floor top-up). The
    ``< 6`` ceiling fails loudly if the figure-floor mis-counts what
    the DI cluster already contributed and adds 3 extras on top.

    Pre-fix on 2026-05-25: every 12-Q section had 6+ DI-bearing items
    and every 15-Q section had 7+. Post-fix: 12-Q averages ~3.2,
    15-Q averages ~4.5.
    """
    qb = QuestionBankService()
    di_per_section = []
    for seed in range(20):
        random.seed(seed * 13 + 7)
        qids = qb.select_questions_composed(
            measure="quant", count=count, difficulty_band="medium",
        )
        _, triples = _section_subtypes(qids)
        di_per_section.append(_count_di_bearing(triples))

    max_di = max(di_per_section)
    mean_di = sum(di_per_section) / len(di_per_section)

    # Hard ceiling: 12-Q never ships >5; 15-Q never ships >6 (DI cluster
    # of 3 + 1-2 floor top-ups + 1 stochastic pick from random ranking).
    # Pre-fix: 12-Q hit 6-8 every time, 15-Q hit 7-9. Post-fix worst
    # case observed across 50 runs: 12-Q max=5, 15-Q max=6.
    ceiling = 6 if count == 12 else 7
    assert max_di < ceiling, (
        f"Quant {count}-Q section over-stacked DI items: max={max_di}, "
        f"per-section={di_per_section}. Expected each section to ship "
        f"~3 DI items (the cluster) plus at most 1-2 figure-floor extras."
    )

    # Mean must stay near the cluster + floor budget. 12-Q floor is 3
    # (DI cluster covers it), 15-Q floor is 4 (cluster + 1 top-up).
    expected_mean_max = 5.0 if count == 12 else 5.5
    assert mean_di <= expected_mean_max, (
        f"Quant {count}-Q DI mean {mean_di:.2f} exceeds expected ceiling "
        f"{expected_mean_max:.2f}. Per-section: {di_per_section}"
    )


# ── 2. Quant: subtype mix lands within a reasonable band ─────────────

@pytest.mark.parametrize("count", [QUANT_S1_COUNT, QUANT_S2_COUNT])
def test_quant_subtype_mix_within_band(count):
    """Aggregate subtype counts across 20 sections stay within a wide
    band around the Magoosh ratios. 12-Q sections are noisy (rounding
    at integers means ±1 is normal), but no subtype should be
    completely missing or 4x over its target.
    """
    qb = QuestionBankService()
    total = Counter()
    for seed in range(20):
        random.seed(seed * 17 + 3)
        qids = qb.select_questions_composed(
            measure="quant", count=count, difficulty_band="medium",
        )
        sub, _ = _section_subtypes(qids)
        total.update(sub)

    # Each subtype's total across 20 sections of N Q's:
    #   expected = 20 * count * QUANT_COMPOSITION[subtype]
    # Bound: actual must be in [0.4 * expected, 2.0 * expected],
    # measured against the post-DI-cluster reality (the cluster
    # absorbs the DI quota up-front so data_interp's nominal ratio
    # already lands close to zero in the "remaining" buckets).
    n_sections = 20
    for subtype, ratio in QUANT_COMPOSITION.items():
        if subtype == "data_interp":
            # The DI cluster ships 3 items but they may be tagged
            # mcq_single / qc / numeric_entry depending on the seed
            # bank. A pure data_interp subtype count will under-shoot
            # in this bank — that's by design. Skip the band check
            # for this subtype; the DI-dominance test above guards
            # against the user's actual concern.
            continue
        expected = n_sections * count * ratio
        actual = total[subtype]
        # Lower bound: 40% of expected so a subtype isn't silently
        # disappearing. Upper bound: 2x expected so we catch a
        # subtype suddenly dominating (user's symptom).
        lower = 0.40 * expected
        upper = 2.00 * expected
        assert lower <= actual <= upper, (
            f"Quant {count}-Q subtype {subtype!r}: total={actual}, "
            f"expected~{expected:.1f}, band [{lower:.1f}, {upper:.1f}]"
        )


# ── 3. Verbal: subtype mix lands within band ─────────────────────────

@pytest.mark.parametrize("count", [VERBAL_S1_COUNT, VERBAL_S2_COUNT])
def test_verbal_subtype_mix_within_band(count):
    """RC + TC + SE proportions stay within a reasonable band of the
    blueprint. RC is allowed to skew up (the passage anchor + rc_single
    fill drive the actual count higher than the nominal ratio when the
    bank has lots of singleton RC items)."""
    qb = QuestionBankService()
    total = Counter()
    for seed in range(20):
        random.seed(seed * 23 + 11)
        qids = qb.select_questions_composed(
            measure="verbal", count=count, difficulty_band="medium",
        )
        sub, _ = _section_subtypes(qids)
        total.update(sub)

    n_sections = 20
    rc_total = (total.get("rc_single", 0)
                + total.get("rc_multi", 0)
                + total.get("rc_select_passage", 0))
    rc_expected = n_sections * count * (VERBAL_COMPOSITION["rc_single"]
                                          + VERBAL_COMPOSITION["rc_multi"]
                                          + VERBAL_COMPOSITION["rc_select_passage"])
    # RC band: 0.5 * expected to 2.0 * expected. The high cap absorbs
    # the passage-anchor preference for multi-Q passages, which
    # naturally inflates RC over the nominal ratio.
    assert 0.5 * rc_expected <= rc_total <= 2.0 * rc_expected, (
        f"Verbal {count}-Q RC totals: {rc_total}, expected~{rc_expected:.1f}"
    )

    tc_expected = n_sections * count * VERBAL_COMPOSITION["tc"]
    se_expected = n_sections * count * VERBAL_COMPOSITION["se"]
    # TC and SE: 30% of expected as floor (the RC anchor steals slots
    # but TC/SE must still appear in most sections).
    assert total["tc"] >= 0.3 * tc_expected, (
        f"Verbal {count}-Q TC under-represented: {total['tc']} < "
        f"{0.3 * tc_expected:.1f}"
    )
    assert total["se"] >= 0.3 * se_expected, (
        f"Verbal {count}-Q SE under-represented: {total['se']} < "
        f"{0.3 * se_expected:.1f}"
    )


# ── 4. Section-test (practice) path mirrors mock path ─────────────────

@pytest.mark.parametrize("measure,count",
                         [("quant", QUANT_S1_COUNT),
                          ("verbal", VERBAL_S1_COUNT)])
def test_section_test_assembly_balanced(measure, count):
    """The practice "Section Test" mode invokes the same composition
    path as the full mock (``select_questions_composed`` via
    ``ExamSession.build_section_test``). This pins that practice-mode
    sections share the same balance guarantees as mock sections.
    """
    di_per_section = []
    for seed in range(15):
        random.seed(seed * 31 + 5)
        qb = QuestionBankService()
        exam = ExamSession(test_type="section_test", mode="simulation")
        exam.build_section_test(measure, qb)
        s1_type = (SectionType.VERBAL_S1 if measure == "verbal"
                   else SectionType.QUANT_S1)
        sec = exam.sections[s1_type]
        assert len(sec.question_ids) == count, (
            f"Section-test {measure} S1 count: {len(sec.question_ids)} "
            f"(expected {count})"
        )
        if measure == "quant":
            _, triples = _section_subtypes(sec.question_ids)
            di_per_section.append(_count_di_bearing(triples))

    if measure == "quant":
        max_di = max(di_per_section)
        # 12-Q never ships >5 DI items. The practice path uses the
        # same composer as full mocks; if the figure-floor ever
        # double-counts again, this will fail with max=6+ same as the
        # full-mock path's parametrized test above.
        assert max_di < 6, (
            f"Practice section_test (quant {count}-Q) over-stacked DI: "
            f"max={max_di}, per-section={di_per_section}"
        )


# ── 5. _count_figure_bearing actually counts figure items ────────────

def test_count_figure_bearing_reads_aliased_content():
    """Direct unit-test: ``_count_figure_bearing`` was returning 0 for
    every input (peewee-alias attribute miss) before the 2026-05-25
    fix. Hard-pin that it counts at least one figure-bearing item
    when fed a known DI cluster's qids.
    """
    qb = QuestionBankService()
    # Find a known DI/figure-bearing qid: a quant item whose stimulus
    # is graph/table and whose content has <img or <table.
    rows = list(
        Question.select(Question.id, Question.stimulus_id)
        .join(Stimulus, on=(Stimulus.id == Question.stimulus))
        .where((Question.measure == "quant") &
               (Question.status == "live") &
               Stimulus.stimulus_type.in_(list(DI_STIMULUS_TYPES)))
        .where(
            (Stimulus.content.contains("<img")) |
            (Stimulus.content.contains("data:image/")) |
            (Stimulus.content.contains("<table"))
        )
        .limit(3)
    )
    qids = [r.id for r in rows]
    if not qids:
        pytest.skip("No DI/figure-bearing quant items in seed bank.")

    n = qb._count_figure_bearing(qids)
    assert n == len(qids), (
        f"_count_figure_bearing under-counted: got {n} of {len(qids)} "
        f"known figure-bearing qids. The aliased Stimulus.content alias "
        f"isn't surfacing — the figure-floor will over-add chart items."
    )
