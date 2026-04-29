"""
Tests for ``QuestionBankService.enforce_cluster_atomicity``.

Quick Drill (and topic drill) picks questions one at a time from per-subtopic
pools without knowing about RC/DI passage clusters. That can leave an orphan
sibling — a single RC question whose 2-3 passage siblings were never picked —
which breaks the real-GRE "every RC question is shown with its passage
siblings" invariant.

The atomicity helper fixes this by either expanding an orphan into the full
cluster (default) or dropping the cluster entirely when expansion would
exceed the caller's budget.
"""
import pytest


def _build_pool(db):
    """Build a small verbal pool with one 3-Q RC cluster + singletons."""
    from models.database import Question, Stimulus

    stim = Stimulus.create(stimulus_type="passage",
                           title="Cluster", content="<p>passage.</p>")
    rc_ids = []
    for i in range(3):
        q = Question.create(
            measure="verbal", subtype="rc_single",
            stimulus=stim, prompt=f"RC-{i}",
            time_target_seconds=90, concept_tags="[]", explanation="",
            difficulty_target=3, status="live",
        )
        rc_ids.append(q.id)

    singleton_ids = []
    for i in range(8):
        q = Question.create(
            measure="verbal", subtype="tc",
            prompt=f"TC-{i}",
            time_target_seconds=60, concept_tags="[]", explanation="",
            difficulty_target=3, status="live",
        )
        singleton_ids.append(q.id)

    return {"rc": rc_ids, "tc": singleton_ids, "stim_id": stim.id}


def test_expand_orphan_rc_pulls_in_siblings(temp_db):
    from services.question_bank import QuestionBankService

    pool = _build_pool(temp_db)
    qb = QuestionBankService()

    # Drill picked one RC orphan + four TC singletons
    ids = [pool["rc"][1]] + pool["tc"][:4]
    out = qb.enforce_cluster_atomicity(
        ids, strict_count=False, max_oversize=10,
    )

    # Every RC sibling is now in the output
    for rc_id in pool["rc"]:
        assert rc_id in out, f"RC sibling {rc_id} missing from cluster expansion"

    # No orphan-only cluster — either all or none.
    rc_present = [q for q in out if q in pool["rc"]]
    assert len(rc_present) == 3


def test_non_cluster_items_are_unchanged(temp_db):
    from services.question_bank import QuestionBankService

    pool = _build_pool(temp_db)
    qb = QuestionBankService()

    ids = pool["tc"][:5]
    out = qb.enforce_cluster_atomicity(ids, strict_count=True, max_oversize=3)

    # Singletons pass through verbatim.
    assert set(out) == set(ids)
    assert len(out) == len(ids)


def test_cluster_dropped_when_budget_exceeded(temp_db):
    """When ``strict_count=True`` and the cluster would overshoot the budget,
    the whole cluster (orphan included) is dropped rather than shipped partial."""
    from services.question_bank import QuestionBankService

    pool = _build_pool(temp_db)
    qb = QuestionBankService()

    # Budget 5; drill included 1 RC orphan + 4 TC. Expanding adds 2 more =>
    # projected 7, which blows past 5 + max_oversize=1 = 6. The cluster
    # should be dropped.
    ids = [pool["rc"][0]] + pool["tc"][:4]
    out = qb.enforce_cluster_atomicity(
        ids, strict_count=True, max_oversize=1,
    )

    # No RC questions remain.
    for rc_id in pool["rc"]:
        assert rc_id not in out, f"Orphan {rc_id} shipped after drop"

    # The TC singletons are still there.
    assert set(pool["tc"][:4]).issubset(set(out))


def test_cluster_atomic_invariant(temp_db):
    """Across many shuffled inputs, the output always contains all-or-none
    of the RC cluster — never a strict subset."""
    import random

    from services.question_bank import QuestionBankService

    pool = _build_pool(temp_db)
    qb = QuestionBankService()
    rc_set = set(pool["rc"])

    for seed in range(10):
        random.seed(seed)
        # Build a random drill that includes 0 / 1 / 2 / 3 RC items.
        n_rc = random.randint(0, 3)
        sample = random.sample(pool["rc"], n_rc) + random.sample(pool["tc"], 5)
        random.shuffle(sample)

        out = qb.enforce_cluster_atomicity(
            sample, strict_count=False, max_oversize=10,
        )
        rc_in_out = rc_set & set(out)
        assert rc_in_out in (set(), rc_set), (
            f"seed {seed}: partial cluster in output {rc_in_out}"
        )


def test_empty_input_returns_empty(temp_db):
    from services.question_bank import QuestionBankService

    qb = QuestionBankService()
    assert qb.enforce_cluster_atomicity([]) == []


def test_siblings_kept_adjacent(temp_db):
    """Cluster siblings land contiguously in the output so the passage
    pane only has to render once per cluster."""
    from services.question_bank import QuestionBankService

    pool = _build_pool(temp_db)
    qb = QuestionBankService()

    # Sprinkle one RC orphan in the middle of some singletons.
    ids = pool["tc"][:2] + [pool["rc"][1]] + pool["tc"][2:4]
    out = qb.enforce_cluster_atomicity(
        ids, strict_count=False, max_oversize=10,
    )

    rc_set = set(pool["rc"])
    rc_positions = [i for i, qid in enumerate(out) if qid in rc_set]
    assert len(rc_positions) == 3
    # Contiguous positions
    assert rc_positions == list(range(rc_positions[0], rc_positions[0] + 3))
