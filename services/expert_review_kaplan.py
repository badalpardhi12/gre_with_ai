"""DEPRECATED — kept as a thin re-export of :mod:`services.expert_review`.

The Kaplan-specific expert review module was merged into the canonical
:mod:`services.expert_review` during the 2026-04-28 data quality sweep.
All names this module used to export are now available from
``services.expert_review``; downstream callers should migrate and drop
the ``_kaplan`` suffix.

Re-exports are preserved so any lingering import (e.g. cached scripts,
unaudited worktrees) keeps working until it's updated.
"""
from services.expert_review import (  # noqa: F401
    DEFECT_TAGS,
    DISAGREEMENT_SPREAD,
    JUDGE_CALL_TIMEOUT_SEC,
    JudgeCallable,
    JudgeReport,
    KAPLAN_DEFAULT_PANEL as DEFAULT_PANEL,
    PROMOTE_MIN_AGREE,
    PROMOTE_MIN_SCORE,
    REVIEW_BLOCK_RE,
    REVIEW_SYSTEM_PROMPT,
    RUBRIC_AXES,
    _parse_judge_response,
    aggregate_verdict,
    build_default_judges,
    build_review_user_message,
    embed_review_in_explanation,
    expert_review_kaplan as expert_review,
    extract_review_from_explanation,
    render_reviewer_notes,
)
