"""
Question-bank scheduler — spaced-repetition + cluster cooldown.

This module refines the crude ``recent_seen_days / mastery_cooldown_days``
filter in ``services.question_bank`` with an FSRS-inspired, cluster-aware
selection algorithm. See ``data/audits/spaced_repetition_research_2026_04_28.md``
for the learning-science backing.

Key ideas
---------
1. **Per-item memory state**, derived on-demand from the append-only
   ``Response`` log (no schema change). Produces (last_seen, times_seen,
   times_correct, last_correct, stability_days, difficulty) for every item
   the user has touched.
2. **Per-cluster cooldown** keyed on ``stimulus_id``. Seeing *any* question
   under a DI chart / RC passage cools down the whole cluster — the user
   remembers the stimulus long after they forget the specific prompt.
   Default DI cooldown = 90d, RC = 45d, tuneable via ``llm_config.json``.
3. **Desirable-difficulty targeting** (Bjork): when the user's rolling
   accuracy is known, prefer items in the 80 % target band, with 20 %
   easier (confidence-building) and 10 % harder (stretch) scattered in.
4. **Leech surfacing**: items answered wrong are eligible again quickly
   (default 3d floor) because productive struggle is the point.
5. **Backward compatibility**: callers can still pass
   ``exclude_user_seen=<user_id>`` to ``select_questions_composed`` — the
   implementation now delegates to this scheduler but preserves the
   legacy ``recent_seen_days`` / ``mastery_cooldown_days`` kwargs as
   aliases for the item cooldown windows.

Implementation notes
--------------------
- Python 3.9 compatible (no ``X | Y`` union types, no ``match``).
- No external dependency beyond peewee (already in the project).
- Scheduler state is derived, not stored. Materialising a cache table
  is a future optimisation; with ~350 responses total today the aggregate
  queries run in single-digit ms.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from peewee import fn

from models.database import Question, Response, Stimulus
from services.log import get_logger

logger = get_logger("scheduler")


# ── Config loading ────────────────────────────────────────────────────

# Default scheduler config. Can be overridden from
# ``data/llm_config.json`` under the ``scheduler`` key, e.g.
#     {"scheduler": {"item_recent_days": 21, "cluster_di_cooldown_days": 120}}
DEFAULT_SCHEDULER_CONFIG: Dict[str, float] = {
    # Window during which an item-level response (right or wrong) is kept
    # out of new selections. Under Ebbinghaus' curve, retention is still
    # strong in the first two weeks; we avoid serving the exact same
    # prompt again inside that window.
    "item_recent_days": 14.0,
    # Cooldown after a *correct* answer — honours the spacing effect but
    # gives the memory long enough to decay below ~30 % before re-serve.
    "item_mastery_cooldown_days": 90.0,
    # Floor for wrong items. Learners benefit from quick re-review under
    # desirable-difficulty theory, but same-day re-exposure is too hot
    # (they'd pattern-match the prompt rather than solve).
    "item_leech_floor_days": 3.0,
    # DI cluster cooldown — chart/table memory decays slowly; 90d matches
    # the ~20 % retention inflection from Murre & Dros (2015).
    "cluster_di_cooldown_days": 90.0,
    # RC cluster cooldown — rereading a passage isn't free even if memory
    # persists, so we allow earlier re-exposure than DI.
    "cluster_rc_cooldown_days": 45.0,
    # Consecutive-session bar. Even if cooldowns have expired, never
    # serve a stimulus from the *immediately previous* session.
    "consecutive_session_bar": True,
    # Target accuracy band for difficulty routing. Within a section,
    # favor items whose difficulty matches the user's ~80 % band.
    "desirable_difficulty_band": 0.80,
    # FSRS-lite defaults — only used when we have > 1 response per item.
    "default_stability_days": 3.0,
    "default_difficulty": 5.0,
    "easy_stability_bonus": 1.3,
    "hard_stability_penalty": 0.5,
    # Score weights (higher positive pushes the item toward selection).
    "w_never_seen": 1.0,
    "w_overdue": 2.5,
    "w_leech": 2.0,
    "w_subtopic_gap": 1.0,
    "w_cluster_recent_penalty": 5.0,
    "w_same_session_penalty": 100.0,
}


def load_scheduler_config() -> Dict[str, float]:
    """Merge ``DEFAULT_SCHEDULER_CONFIG`` with overrides from
    ``data/llm_config.json`` under key ``scheduler``.

    Re-read on every call so the settings surface can hot-swap a cooldown
    without an app restart.
    """
    # Lazy import: config imports must stay lazy because some test fixtures
    # swap ``config.DB_PATH`` after this module is already loaded.
    from config import load_llm_config

    merged = dict(DEFAULT_SCHEDULER_CONFIG)
    raw = load_llm_config().get("scheduler")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in merged and isinstance(v, (int, float, bool)):
                merged[k] = v
    return merged


# ── Data structures ───────────────────────────────────────────────────


@dataclass
class ItemMemoryState:
    """Derived memory state for a single question.

    All values are *derived* from the Response log — nothing is persisted
    in a dedicated table. Stability and difficulty are estimated by a
    simplified FSRS-lite update; they converge toward defaults when the
    item has few reviews.
    """
    question_id: int
    last_seen: Optional[datetime] = None
    last_correct: Optional[datetime] = None
    times_seen: int = 0
    times_correct: int = 0
    stability_days: float = 3.0
    difficulty: float = 5.0  # 1..10; higher = harder for this user

    @property
    def p_correct(self) -> float:
        """Rolling hit rate. Returns 0.5 when the item is unseen."""
        if self.times_seen == 0:
            return 0.5
        return self.times_correct / self.times_seen

    def retrievability(self, now: Optional[datetime] = None) -> float:
        """FSRS-style retrievability at time ``now``.

        R(t, S) = (1 + FACTOR * t / S) ** DECAY
        with DECAY = -0.5 and FACTOR = 19/81 so R(S) = 0.9 exactly.
        """
        if self.last_seen is None or self.stability_days <= 0:
            return 0.0
        now = now or datetime.now()
        t = max(0.0, (now - self.last_seen).total_seconds() / 86400.0)
        factor = 19.0 / 81.0
        return (1.0 + factor * t / self.stability_days) ** -0.5

    def days_since_seen(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.last_seen is None:
            return None
        now = now or datetime.now()
        return (now - self.last_seen).total_seconds() / 86400.0


@dataclass
class ClusterExposure:
    """Per-stimulus exposure record: when was *any* child question last
    served, and how many sibling questions have been touched to date."""
    stimulus_id: int
    last_seen: Optional[datetime] = None
    exposures: int = 0
    last_session_id: Optional[int] = None


@dataclass
class UserProfile:
    """Everything the scheduler needs to score items for a single user.

    Built once per ``select_questions_composed`` call; cheap aggregate
    queries so rebuild-every-call is fine up to ~10k responses."""
    user_id: str
    items: Dict[int, ItemMemoryState] = field(default_factory=dict)
    clusters: Dict[int, ClusterExposure] = field(default_factory=dict)
    last_session_id: Optional[int] = None
    rolling_accuracy: float = 0.5  # 50 % default for first-time user


# ── Building the profile ──────────────────────────────────────────────


def _fsrs_lite_update(state: ItemMemoryState, is_correct: bool) -> None:
    """Mutate ``state`` in place with a one-step SM-2/FSRS hybrid update.

    This is intentionally simple — we have <1k responses total in the DB
    today, not enough to fit real FSRS weights. The defaults below follow
    the FSRS v4 paper's shapes but with hand-picked constants.
    """
    state.times_seen += 1
    if is_correct:
        state.times_correct += 1
        # Reward for retrieval under conditions of decay: S grows more
        # when the review was overdue. We approximate "overdue" by
        # dividing interval since last_seen by current stability.
        if state.last_seen is not None and state.stability_days > 0:
            r = state.retrievability()
            # Bounded multiplier: 1.3 at R=0.9, up to ~3.0 at R=0.5
            mult = 1.0 + max(0.2, (1.0 - r) * 3.0)
        else:
            mult = 2.5  # "graduating" bump for a first correct
        state.stability_days = max(1.0, state.stability_days * mult)
        # Difficulty drifts down slowly on success.
        state.difficulty = max(1.0, state.difficulty - 0.2)
    else:
        # Lapse: collapse stability (but keep a fraction per FSRS, not SM-2
        # reset to zero) and bump difficulty.
        state.stability_days = max(1.0, state.stability_days * 0.4)
        state.difficulty = min(10.0, state.difficulty + 0.8)


def build_user_profile(user_id: str = "local") -> UserProfile:
    """Scan the Response log and produce a UserProfile.

    Complexity: O(N) where N is this user's response count. For the
    single-user GRE prep app with a few hundred responses this is
    millisecond-scale; once response counts exceed ~50k we should
    materialise `ItemMemoryState` into a table updated on Response insert.
    """
    profile = UserProfile(user_id=user_id)
    cfg = load_scheduler_config()
    default_s = float(cfg["default_stability_days"])
    default_d = float(cfg["default_difficulty"])

    # Pull responses chronologically so our incremental FSRS-lite updates
    # see them in the right order.
    rows = (
        Response
        .select(Response.question_id, Response.is_correct,
                Response.created_at, Response.session_id, Response.time_spent_seconds)
        .order_by(Response.created_at.asc())
    )

    most_recent_session_id = None
    correct_count = 0
    total_count = 0

    for r in rows:
        qid = r.question_id
        state = profile.items.get(qid)
        if state is None:
            state = ItemMemoryState(
                question_id=qid,
                stability_days=default_s,
                difficulty=default_d,
            )
            profile.items[qid] = state
        state.last_seen = r.created_at
        if r.is_correct:
            state.last_correct = r.created_at

        _fsrs_lite_update(state, bool(r.is_correct))

        # Track rolling accuracy across all user responses (with answers).
        if r.is_correct is not None:
            total_count += 1
            if r.is_correct:
                correct_count += 1

        most_recent_session_id = r.session_id

    profile.last_session_id = most_recent_session_id
    if total_count > 0:
        profile.rolling_accuracy = correct_count / total_count

    # Build per-cluster exposure by joining items → question.stimulus_id.
    if profile.items:
        qids = list(profile.items.keys())
        # Batch in chunks to dodge SQLite's 999-param limit on big histories.
        CHUNK = 800
        qid_to_stim: Dict[int, Optional[int]] = {}
        for i in range(0, len(qids), CHUNK):
            chunk = qids[i:i + CHUNK]
            qrows = Question.select(Question.id, Question.stimulus_id).where(
                Question.id.in_(chunk)
            )
            for q in qrows:
                qid_to_stim[q.id] = q.stimulus_id

        last_session_per_stim: Dict[int, int] = {}
        for qid, state in profile.items.items():
            stim_id = qid_to_stim.get(qid)
            if not stim_id:
                continue
            ce = profile.clusters.get(stim_id)
            if ce is None:
                ce = ClusterExposure(stimulus_id=stim_id)
                profile.clusters[stim_id] = ce
            if state.last_seen is not None:
                if ce.last_seen is None or state.last_seen > ce.last_seen:
                    ce.last_seen = state.last_seen
            ce.exposures += state.times_seen

        # Fill in last_session_id per cluster (so we can enforce the
        # "never repeat across consecutive sessions" rule).
        if last_session_per_stim:
            for stim_id, sess_id in last_session_per_stim.items():
                if stim_id in profile.clusters:
                    profile.clusters[stim_id].last_session_id = sess_id

    return profile


def _cluster_last_session(user_id: str = "local") -> Dict[int, int]:
    """For each stimulus the user has responded to, return the *most
    recent* session_id that touched it. Computed separately from
    build_user_profile so tests can monkey-patch cleanly."""
    rows = (
        Response
        .select(Question.stimulus_id.alias("stim"),
                fn.MAX(Response.session_id).alias("sess"))
        .join(Question, on=(Question.id == Response.question_id))
        .where(Question.stimulus_id.is_null(False))
        .group_by(Question.stimulus_id)
    )
    out: Dict[int, int] = {}
    for r in rows:
        stim = r.stim
        if stim is not None and r.sess is not None:
            out[int(stim)] = int(r.sess)
    return out


# ── Exclusion + scoring API ───────────────────────────────────────────


def compute_exclusions(
    profile: UserProfile,
    now: Optional[datetime] = None,
    config: Optional[Dict[str, float]] = None,
) -> Tuple[Set[int], Set[int], Set[int]]:
    """Compute three mutually-exclusive suppression sets.

    Returns ``(hard_exclude_qids, cluster_cooled_stim_ids, soft_cooled_qids)``:

    - ``hard_exclude_qids``: individual qids inside the recent-seen window
      or the correct-mastery window. These are dedup filters — never served.
    - ``cluster_cooled_stim_ids``: stimulus IDs inside a DI/RC cluster
      cooldown. Every child question under one of these must be filtered
      out, even if the specific qid was never seen.
    - ``soft_cooled_qids``: items the user answered CORRECTLY but which
      fell outside the recent-seen window AND the mastery window. We
      deprioritise them but don't ban them — they become eligible as the
      pool thins.
    """
    cfg = config or load_scheduler_config()
    now = now or datetime.now()

    item_recent = float(cfg["item_recent_days"])
    mastery = float(cfg["item_mastery_cooldown_days"])
    leech_floor = float(cfg["item_leech_floor_days"])
    di_cd = float(cfg["cluster_di_cooldown_days"])
    rc_cd = float(cfg["cluster_rc_cooldown_days"])

    hard: Set[int] = set()
    soft: Set[int] = set()
    for qid, state in profile.items.items():
        days = state.days_since_seen(now)
        if days is None:
            continue
        # Recent-seen: suppress entirely.
        if days < item_recent:
            hard.add(qid)
            continue
        # Correct-mastery: suppress while memory is likely to be strong.
        if state.last_correct is not None:
            days_since_correct = (
                (now - state.last_correct).total_seconds() / 86400.0
            )
            if days_since_correct < mastery:
                hard.add(qid)
                continue
        # Leech floor: even items answered wrong need a short cooldown —
        # if the user saw it < 3 days ago we already caught it in the
        # recent-seen block above, so this branch is defensive.
        if days < leech_floor and state.times_correct == 0:
            hard.add(qid)
            continue
        # Past mastery window but previously correct: soft-cooled.
        if state.last_correct is not None:
            soft.add(qid)

    # Cluster cooldowns — gather stimulus_type once per cluster so we can
    # apply the DI/RC-specific window.
    cluster_cooled: Set[int] = set()
    if profile.clusters:
        stim_ids = list(profile.clusters.keys())
        CHUNK = 800
        stim_type: Dict[int, str] = {}
        for i in range(0, len(stim_ids), CHUNK):
            sub = stim_ids[i:i + CHUNK]
            for s in Stimulus.select(Stimulus.id, Stimulus.stimulus_type).where(
                Stimulus.id.in_(sub)
            ):
                stim_type[s.id] = s.stimulus_type or ""

        for stim_id, ce in profile.clusters.items():
            if ce.last_seen is None:
                continue
            age_days = (now - ce.last_seen).total_seconds() / 86400.0
            kind = stim_type.get(stim_id, "")
            if kind in ("graph", "table", "chart"):
                window = di_cd
            elif kind == "passage":
                window = rc_cd
            else:
                window = rc_cd  # default to the shorter window
            if age_days < window:
                cluster_cooled.add(stim_id)

    return hard, cluster_cooled, soft


def score_item(
    question_row,
    profile: UserProfile,
    config: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
    target_difficulty: Optional[int] = None,
    current_session_stim_ids: Optional[Set[int]] = None,
) -> float:
    """Priority score for a single candidate (higher = more desirable).

    ``question_row`` must expose ``.id``, ``.stimulus_id``, and
    ``.difficulty_target`` — a plain peewee row works.

    Intended for use after ``compute_exclusions`` has already filtered
    out items in the hard cooldown. Soft-cooled items get a negative
    contribution so they slot in last.
    """
    cfg = config or load_scheduler_config()
    now = now or datetime.now()
    qid = question_row.id
    stim_id = getattr(question_row, "stimulus_id", None)
    diff = getattr(question_row, "difficulty_target", 3) or 3

    state = profile.items.get(qid)

    score = 0.0
    if state is None:
        # Never-seen: add an exploration bonus so fresh items lead.
        score += float(cfg["w_never_seen"])
    else:
        r = state.retrievability(now)
        # Overdueness: an item whose retrievability has dropped well
        # below 0.9 is prime for review under FSRS desirable-difficulty.
        overdueness = max(0.0, 0.9 - r)
        score += float(cfg["w_overdue"]) * overdueness
        # Items answered wrong get a leech bonus so they circulate back.
        if state.times_correct == 0 and state.times_seen > 0:
            score += float(cfg["w_leech"])
        # Previously-mastered items sit below never-seen items.
        if state.last_correct is not None:
            score -= 0.2

    # Difficulty targeting: prefer items at the user's 80 % band. We
    # don't *exclude* off-band items — just nudge toward band.
    if target_difficulty is not None:
        delta = abs(int(diff) - int(target_difficulty))
        # Every step off-band costs 0.3 — 2 full steps gives -0.6.
        score -= 0.3 * delta

    # Cluster recency penalty (soft — if the item isn't in the cluster
    # hard-exclude set it's already past cooldown; but pull-forward bias
    # still benefits from deprioritising recently-seen stimuli).
    if stim_id is not None:
        ce = profile.clusters.get(stim_id)
        if ce and ce.last_seen is not None:
            age_days = (now - ce.last_seen).total_seconds() / 86400.0
            # Gentle decay — a cluster seen 100 days ago still gets a
            # small penalty so we prefer genuinely novel stimuli.
            score -= float(cfg["w_cluster_recent_penalty"]) * math.exp(
                -age_days / 180.0
            )

    # Same-session bar (absolute): if the caller has already picked
    # something from this stimulus for the CURRENT session, hard-push.
    if current_session_stim_ids and stim_id in current_session_stim_ids:
        score -= float(cfg["w_same_session_penalty"])

    return score


def target_difficulty_for(profile: UserProfile,
                          config: Optional[Dict[str, float]] = None) -> int:
    """Map rolling accuracy to a difficulty_target (1..5) such that
    selected items land in the user's ~80 % desirable-difficulty band.

    - accuracy ≥ 0.85 → difficulty 4 (push harder)
    - 0.70 ≤ accuracy < 0.85 → difficulty 3 (sweet spot — medium)
    - 0.55 ≤ accuracy < 0.70 → difficulty 3 still, nudge easy via 20 % band
    - accuracy < 0.55 → difficulty 2 (confidence-build)
    """
    acc = profile.rolling_accuracy
    if acc >= 0.85:
        return 4
    if acc >= 0.70:
        return 3
    if acc >= 0.55:
        return 3
    return 2


# ── Public façade used by question_bank ───────────────────────────────


def scheduler_exclusions(
    user_id: str,
    recent_seen_days: Optional[float] = None,
    mastery_cooldown_days: Optional[float] = None,
    cluster_di_days: Optional[float] = None,
    cluster_rc_days: Optional[float] = None,
) -> Tuple[Set[int], Set[int]]:
    """Backward-compatible wrapper used by ``select_questions_composed``.

    Returns ``(qids_to_exclude, stim_ids_to_exclude)``. The caller adds
    both to its exclusion set before running the selection pipeline.

    Kwargs override the defaults from ``llm_config.json``. Passing 0 or
    None disables that particular window.
    """
    cfg = dict(load_scheduler_config())
    if recent_seen_days is not None:
        cfg["item_recent_days"] = float(recent_seen_days)
    if mastery_cooldown_days is not None:
        cfg["item_mastery_cooldown_days"] = float(mastery_cooldown_days)
    if cluster_di_days is not None:
        cfg["cluster_di_cooldown_days"] = float(cluster_di_days)
    if cluster_rc_days is not None:
        cfg["cluster_rc_cooldown_days"] = float(cluster_rc_days)

    profile = build_user_profile(user_id=user_id)
    hard_qids, cluster_stims, _soft = compute_exclusions(profile, config=cfg)
    return hard_qids, cluster_stims


def stim_ids_to_qids(stim_ids: Iterable[int]) -> Set[int]:
    """Expand a set of stimulus IDs into the set of every live child
    question under any of them. Convenience for callers that want to
    feed a flat qid exclusion list into a legacy selector."""
    sid_list = [int(x) for x in stim_ids]
    if not sid_list:
        return set()
    out: Set[int] = set()
    CHUNK = 800
    for i in range(0, len(sid_list), CHUNK):
        sub = sid_list[i:i + CHUNK]
        for q in Question.select(Question.id).where(
            Question.stimulus_id.in_(sub),
            Question.status == "live",
        ):
            out.add(q.id)
    return out


def next_due_items(
    user_id: str,
    n: int,
    measure: str,
    difficulty_band: str = "medium",
    extra_exclude_qids: Optional[Iterable[int]] = None,
    current_session_stim_ids: Optional[Set[int]] = None,
) -> List[int]:
    """Return up to ``n`` question IDs for ``measure``, ranked by the
    scheduler's priority score.

    Hard exclusions (recent-seen, mastery-cooled, cluster-cooled, caller-
    supplied) are enforced. Ties are broken deterministically by ``qid``
    — callers that want shuffling should shuffle the return.
    """
    profile = build_user_profile(user_id=user_id)
    hard_qids, cluster_stims, _soft = compute_exclusions(profile)
    hard_qids |= set(extra_exclude_qids or [])
    # Expand cluster cooldown to child qids.
    hard_qids |= stim_ids_to_qids(cluster_stims)

    # Pull a difficulty-band-scoped pool.
    query = Question.select(
        Question.id, Question.subtype,
        Question.stimulus_id, Question.difficulty_target,
    ).where(
        (Question.measure == measure) & (Question.status == "live")
    )
    if difficulty_band == "easy":
        query = query.where(Question.difficulty_target <= 2)
    elif difficulty_band == "hard":
        query = query.where(Question.difficulty_target >= 4)
    if hard_qids:
        query = query.where(Question.id.not_in(list(hard_qids)))

    target_diff = target_difficulty_for(profile)
    scored: List[Tuple[float, int]] = []
    for q in query:
        s = score_item(
            q, profile,
            target_difficulty=target_diff,
            current_session_stim_ids=current_session_stim_ids,
        )
        scored.append((s, q.id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [qid for _s, qid in scored[:n]]


__all__ = [
    "DEFAULT_SCHEDULER_CONFIG",
    "load_scheduler_config",
    "ItemMemoryState",
    "ClusterExposure",
    "UserProfile",
    "build_user_profile",
    "compute_exclusions",
    "scheduler_exclusions",
    "stim_ids_to_qids",
    "score_item",
    "target_difficulty_for",
    "next_due_items",
]
