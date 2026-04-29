"""
Manual scenario test — build 10 back-to-back full mock exams for
user_id='local' against the real gre_user.db, count DI/RC cluster
overlaps, and dump the diagnostic. This is NOT part of the unit test
suite (it reads the real DB), but is runnable with

    venv/bin/python scripts/scenario_mock_overlap.py

Output goes to stdout + ``data/audits/mock_overlap_scenario.md``.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Make the project importable from this script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Redirect DB to a temp scratch copy so we don't pollute gre_user.db.
import shutil
import tempfile
import config as _config
_tmpdir = Path(tempfile.mkdtemp(prefix="scenario_"))
_scratch_db = _tmpdir / "scenario.db"
shutil.copy2(str(_config.DB_PATH), str(_scratch_db))
_config.DB_PATH = _scratch_db
print(f"[scenario] using scratch DB copy at {_scratch_db}")

from models.database import Question, Response, Stimulus, init_db  # noqa: E402
from services.question_bank import QuestionBankService  # noqa: E402


def _stim_for(qid):
    q = Question.get_or_none(Question.id == qid)
    return q.stimulus_id if q else None


def _cluster_sig(qids):
    """Return the set of (stimulus_type, stimulus_id) clusters present."""
    clusters = set()
    for qid in qids:
        q = Question.get_or_none(Question.id == qid)
        if not q or q.stimulus_id is None:
            continue
        stim = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
        if stim is None:
            continue
        clusters.add((stim.stimulus_type, stim.id))
    return clusters


def _simulate_responses(qids, session_id, hit_rate=0.7):
    """Drop synthetic Response rows to mimic a user who actually solved
    the section. Without this the scheduler has nothing to cool down."""
    from models.database import SectionResult, Response
    import random
    now = datetime.now()
    # Create a synthetic SectionResult so FK is satisfied.
    # NOTE: needs a parent session — passed in as session_id.
    secr = SectionResult.create(
        session_id=session_id, section_name="quant_s1",
        measure="quant", section_index=1,
        time_limit_seconds=1080, question_ids="[]",
    )
    for qid in qids:
        is_correct = random.random() < hit_rate
        Response.create(
            session_id=session_id, section_result=secr,
            question_id=qid, response_payload="{}",
            is_marked=False, is_correct=is_correct,
            time_spent_seconds=60,
            answered_at=now, created_at=now,
        )


def _new_session():
    from models.database import Session
    return Session.create(
        test_type="full_mock", mode="simulation",
        section_order="[]", current_section_index=0,
        state="completed",
    ).id


def run(n_mocks: int = 10, simulate: bool = True):
    init_db()
    qb = QuestionBankService()

    per_mock = []
    for i in range(n_mocks):
        # Build two quant sections + two verbal sections back-to-back.
        quant_s1 = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium",
            exclude_user_seen="local",
        )
        quant_s2 = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="medium",
            exclude_ids=quant_s1, exclude_user_seen="local",
        )
        verbal_s1 = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium",
            exclude_user_seen="local",
        )
        verbal_s2 = qb.select_questions_composed(
            measure="verbal", count=15, difficulty_band="medium",
            exclude_ids=verbal_s1, exclude_user_seen="local",
        )

        all_qids = quant_s1 + quant_s2 + verbal_s1 + verbal_s2
        per_mock.append({
            "mock": i + 1,
            "qids": all_qids,
            "clusters": _cluster_sig(all_qids),
            "size": len(all_qids),
        })

        if simulate and all_qids:
            sess_id = _new_session()
            _simulate_responses(all_qids, sess_id)

    # Pairwise overlap matrix.
    print(f"Built {n_mocks} mocks.\n")
    print("Per-mock size / DI+RC cluster counts:")
    for m in per_mock:
        di = sum(1 for k in m["clusters"] if k[0] in ("graph", "table", "chart"))
        rc = sum(1 for k in m["clusters"] if k[0] == "passage")
        print(f"  mock {m['mock']}: {m['size']} qs, DI clusters={di}, RC clusters={rc}")

    print("\nPairwise cluster overlap counts (shared clusters between two mocks):")
    for i in range(n_mocks):
        for j in range(i + 1, n_mocks):
            overlap = per_mock[i]["clusters"] & per_mock[j]["clusters"]
            if overlap:
                di_over = sum(1 for k in overlap if k[0] in ("graph", "table", "chart"))
                rc_over = sum(1 for k in overlap if k[0] == "passage")
                print(f"  mocks {i+1} vs {j+1}: total={len(overlap)}, DI={di_over}, RC={rc_over}")

    # Cluster exposure over the full run.
    cluster_counts = Counter()
    for m in per_mock:
        for k in m["clusters"]:
            cluster_counts[k] += 1
    repeated = {k: v for k, v in cluster_counts.items() if v > 1}
    print(f"\nClusters appearing in MORE than one mock: {len(repeated)}")
    if repeated:
        print("Top 10 repeated clusters:")
        for (kind, stim_id), count in sorted(repeated.items(), key=lambda x: -x[1])[:10]:
            print(f"  {kind} stim#{stim_id}: {count} mocks")


if __name__ == "__main__":
    n = 10
    if len(sys.argv) > 1:
        n = int(sys.argv[1])
    run(n_mocks=n)
