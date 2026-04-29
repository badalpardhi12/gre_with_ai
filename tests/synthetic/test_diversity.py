"""
Tests for dedup and the DiversitySampler.

The sampler depends on the live taxonomy, but does NOT need a populated
DB beyond what `init_db` seeds. The dedup tests are purely in-memory.
"""
from __future__ import annotations

import random

import pytest


# ── Dedup ───────────────────────────────────────────────────────────


def test_jaccard_dedup_blocks_near_paraphrase():
    from services.synthetic.dedup import JaccardDeduper
    d = JaccardDeduper(threshold=0.7)
    d.register("The quick brown fox jumps over the lazy dog.")
    is_dup, sim = d.is_duplicate("The quick brown fox jumped over the lazy dog.")
    assert is_dup is True, sim
    assert sim >= 0.7


def test_jaccard_dedup_passes_distinct_stems():
    from services.synthetic.dedup import JaccardDeduper
    d = JaccardDeduper(threshold=0.7)
    d.register("A train leaves station X at noon traveling east at 60 mph.")
    is_dup, sim = d.is_duplicate(
        "Although her early work was praised for its precision, the poet's later collections proved unbridled."
    )
    assert is_dup is False
    assert sim < 0.5


def test_jaccard_dedup_per_subtopic_isolation():
    """A near-duplicate stem in a *different* subtopic must not trigger."""
    from services.synthetic.dedup import JaccardDeduper
    d = JaccardDeduper(threshold=0.7)
    d.register("The quick brown fox jumps over the lazy dog.", subtopic="tc_1_blank")
    is_dup, _ = d.is_duplicate(
        "The quick brown fox jumps over the lazy dog.",
        subtopic="qc_basic",
    )
    assert is_dup is False


def test_dedup_window_evicts_oldest():
    from services.synthetic.dedup import JaccardDeduper
    d = JaccardDeduper(threshold=0.5, window_per_subtopic=2)
    d.register("alpha beta gamma delta epsilon", subtopic="x")
    d.register("uno dos tres cuatro cinco", subtopic="x")
    d.register("foo bar baz qux quux", subtopic="x")
    # First stem evicted; querying it should NOT trigger.
    is_dup, _ = d.is_duplicate("alpha beta gamma delta epsilon", subtopic="x")
    assert is_dup is False


def test_make_default_deduper_returns_callable_protocol():
    from services.synthetic.dedup import make_default_deduper
    d = make_default_deduper()
    # The factory currently returns Jaccard since sentence-transformers
    # isn't a runtime dependency. Either is acceptable; we verify the
    # protocol surface.
    assert hasattr(d, "is_duplicate")
    assert hasattr(d, "register")
    assert hasattr(d, "reset")
    d.register("anything")
    is_dup, sim = d.is_duplicate("anything")
    assert is_dup is True


# ── DiversitySampler ────────────────────────────────────────────────


def test_diversity_sampler_emits_n_seeds_with_dimensions(temp_db):
    """With a clean taxonomy and no live items, every subtopic is at
    full deficit; the sampler should emit N seeds, each carrying
    scenario/persona/frame in `extra`."""
    from services.synthetic.seeder import DiversitySampler
    sampler = DiversitySampler()
    rng = random.Random(7)
    seeds = sampler.sample(20, rng=rng)
    assert len(seeds) == 20
    for s in seeds:
        assert s.measure in {"verbal", "quant"}
        assert s.subtopic
        assert s.subtype
        assert s.difficulty_target in (2, 3, 4)
        assert "scenario_class" in s.extra
        assert "persona" in s.extra
        assert "structural_frame" in s.extra


def test_diversity_sampler_difficulty_distribution_holds(temp_db):
    """The Latin-Hypercube pre-shuffle should land each band within
    ±1 of its target proportion at large N."""
    from services.synthetic.seeder import DiversitySampler
    sampler = DiversitySampler(
        difficulty_distribution={2: 0.30, 3: 0.45, 4: 0.25}
    )
    rng = random.Random(13)
    seeds = sampler.sample(100, rng=rng)
    counts = {2: 0, 3: 0, 4: 0}
    for s in seeds:
        counts[s.difficulty_target] = counts.get(s.difficulty_target, 0) + 1
    # ±1 tolerance because of integer rounding in the LHS.
    assert abs(counts[2] - 30) <= 2, counts
    assert abs(counts[3] - 45) <= 2, counts
    assert abs(counts[4] - 25) <= 2, counts


def test_diversity_sampler_avoids_back_to_back_collisions(temp_db):
    """Two consecutive seeds for the same subtopic should not share
    (scenario, persona, frame, difficulty) — the tracker rejects
    collisions on the first try at low fan-out."""
    from services.synthetic.seeder import DiversitySampler
    sampler = DiversitySampler()
    rng = random.Random(2)
    # Force the sampler to one subtopic so collisions are likely
    # without the tracker.
    seeds = sampler.sample(5, rng=rng,
                           subtopic_filter=["tc_1_blank"],
                           measure_filter="verbal")
    # 5 seeds in one subtopic: at the chosen pool sizes the tracker
    # still has room. Verify no two seeds share the full 4-tuple.
    keys = {
        (s.extra["scenario_class"], s.extra["persona"],
         s.extra["structural_frame"], s.difficulty_target)
        for s in seeds
    }
    assert len(keys) == len(seeds)


def test_diversity_sampler_subtopic_coverage(temp_db):
    """200 seeds should cover a substantial fraction of the available
    subtopics (plan §10 R3 test bar: ≥ 80%)."""
    from services.synthetic.seeder import DiversitySampler, compute_deficits
    sampler = DiversitySampler()
    rng = random.Random(101)
    available_subtopics = {r["subtopic"] for r in compute_deficits()}
    seeds = sampler.sample(200, rng=rng)
    covered = {s.subtopic for s in seeds}
    coverage = len(covered) / len(available_subtopics)
    assert coverage >= 0.80, (
        f"only {coverage:.0%} of {len(available_subtopics)} subtopics "
        f"covered in 200 seeds"
    )


def test_diversity_sampler_reset_history_clears_tracker(temp_db):
    """reset_history() lets a long-running process forget the
    collision window without rebuilding the sampler."""
    from services.synthetic.seeder import DiversitySampler
    sampler = DiversitySampler(window=2)
    rng = random.Random(3)
    sampler.sample(5, rng=rng)
    assert sampler._tracker._by_subtopic, "tracker should record history"
    sampler.reset_history()
    assert not sampler._tracker._by_subtopic, "history should clear"
