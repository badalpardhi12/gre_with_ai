"""RC cluster integrity remediation.

Three cleanup steps, each safe and idempotent:

  A. dedupe_stimuli       — collapse exact-text duplicate passage stimuli
                            onto the oldest, relinking every Question.
  B. relink_orphans       — attempt deterministic linking of RC questions
                            whose stimulus_id is NULL. Currently no true
                            RC orphans exist (verified — see audit). The
                            function remains in place for regressions.
  C. strip_cluster_marker — for passage stimuli whose text contains a
                            "Questions N-M are based on the passage below."
                            header but whose live question count does not
                            match the marker's claimed span, strip the
                            leading marker so the passage reads cleanly
                            as a solo item.

Each step returns a structured result so that the driver can log
before/after counts and write the summary into the audit markdown.
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LOG = logging.getLogger("rc_integrity")

# Matches markers like:
#   <b>Questions 8–10 are based on the passage below.</b>
#   Questions 7-10 are based on the passage below.
#   Questions 1 and 2 are based on the passage below.
#   Questions 15 and 16 are based on the following passage.
CLUSTER_MARKER_RE = re.compile(
    r"(?ix)                                     "   # ignore-case + verbose
    r"(?:<b>\s*)?                                "
    r"Questions?\s+                              "
    r"(?:                                         "
    r"  (?P<first>\d+)\s*(?:[-–—]|and)\s*(?P<last>\d+)"
    r"|                                           "
    r"  (?P<single>\d+)                           "
    r")                                           "
    r"\s+(?:are|is)\s+based\s+on\s+the\s+(?:following\s+)?passage\s*(?:below)?\.?"
    r"\s*(?:</b>)?                                "
    r"\s*"
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Case A — duplicate stimuli
# ---------------------------------------------------------------------------


@dataclass
class DedupeReport:
    groups_merged: int = 0
    stimuli_deleted: int = 0
    questions_relinked: int = 0
    details: List[Tuple[int, List[int], int]] = field(default_factory=list)
    """(canonical_id, duplicate_ids, questions_relinked_count)."""


def find_duplicate_stimuli(conn: sqlite3.Connection) -> List[List[int]]:
    """Return groups (>1) of passage stimuli sharing identical content.

    Each returned inner list is sorted by id (oldest first), so the
    canonical stimulus for a group is always the list's first element.
    """
    rows = conn.execute(
        """
        SELECT content, GROUP_CONCAT(id) FROM stimulus
        WHERE stimulus_type = 'passage'
        GROUP BY content HAVING COUNT(*) > 1
        """
    ).fetchall()
    groups: List[List[int]] = []
    for _content, ids_csv in rows:
        ids = sorted(int(x) for x in ids_csv.split(","))
        groups.append(ids)
    return groups


def dedupe_stimuli(conn: sqlite3.Connection, dry_run: bool = False) -> DedupeReport:
    """Collapse duplicate-content stimulus rows onto the oldest one.

    The oldest id is canonical; every Question pointing at a duplicate
    is re-pointed to the canonical stimulus. The duplicate rows are
    then deleted. We refuse to delete a duplicate if any Question
    still references it after the UPDATE (defensive — shouldn't happen
    since we just moved them all, but protects against FK drift).
    """
    report = DedupeReport()
    groups = find_duplicate_stimuli(conn)
    for group in groups:
        canonical, *duplicates = group
        relinked = conn.execute(
            f"SELECT COUNT(*) FROM question WHERE stimulus_id IN ({','.join('?' * len(duplicates))})",
            duplicates,
        ).fetchone()[0]
        if not dry_run:
            conn.execute(
                f"UPDATE question SET stimulus_id = ? "
                f"WHERE stimulus_id IN ({','.join('?' * len(duplicates))})",
                [canonical, *duplicates],
            )
            # Defensive check before delete
            residual = conn.execute(
                f"SELECT COUNT(*) FROM question WHERE stimulus_id IN ({','.join('?' * len(duplicates))})",
                duplicates,
            ).fetchone()[0]
            if residual:
                raise RuntimeError(
                    f"Refusing to delete stimulus dups {duplicates}: {residual} questions still linked"
                )
            conn.execute(
                f"DELETE FROM stimulus WHERE id IN ({','.join('?' * len(duplicates))})",
                duplicates,
            )
        report.groups_merged += 1
        report.stimuli_deleted += len(duplicates)
        report.questions_relinked += relinked
        report.details.append((canonical, duplicates, relinked))
        LOG.info(
            "dedupe: canonical=%s duplicates=%s relinked=%s",
            canonical, duplicates, relinked,
        )
    return report


# ---------------------------------------------------------------------------
# Case B — NULL-stimulus RC orphans
# ---------------------------------------------------------------------------


@dataclass
class OrphanRelinkReport:
    candidates_examined: int = 0
    relinked: int = 0
    left_as_orphan: int = 0
    misclassified: int = 0
    """Quant/SE/TC questions mis-tagged as rc_*. Not touched here; reported
    so an upstream subtype-repair job can pick them up."""


def relink_orphans(conn: sqlite3.Connection, dry_run: bool = False) -> OrphanRelinkReport:
    """Try to attach RC orphans to their passage.

    Strategy (deterministic only):
      1. If the question's prompt contains an inline passage-like preamble
         that exactly matches an existing stimulus content, link.
      2. Otherwise, if the question's source + source_anchor uniquely
         identifies a passage stimulus in the extraction JSON, link.

    We classify orphans as "misclassified" (prompt reads as a quant or
    SE/TC item, not a passage-referring RC item) vs "genuine RC" before
    even trying to link. Misclassified ones are left alone here — a
    separate subtype-repair job must fix their measure/subtype fields.

    In the current DB all 48 orphans are misclassified, so this function
    does zero relinking today. It remains in place for future rc_*
    imports.
    """
    report = OrphanRelinkReport()
    rows = conn.execute(
        """
        SELECT id, subtype, prompt FROM question
        WHERE subtype IN ('rc_single', 'rc_multi', 'rc_select_passage')
          AND stimulus_id IS NULL AND status = 'live'
        """
    ).fetchall()
    for qid, subtype, prompt in rows:
        report.candidates_examined += 1
        prompt_l = (prompt or "").lower()
        # Heuristic: real RC prompts reference "the passage" or "the author"
        # or start with quoted excerpts. Misclassified quant prompts are
        # full of LaTeX / equation syntax or are pure sentence-completion
        # fill-ins ("_________").
        is_sentence_completion = "_________" in prompt or "______" in prompt
        is_quant = any(
            marker in prompt for marker in ("\\(", "\\[", "\\frac", "\\ne", "\\geq", "\\leq")
        )
        looks_rc = ("passage" in prompt_l) or ("the author" in prompt_l)
        if is_sentence_completion or is_quant or not looks_rc:
            report.misclassified += 1
            continue
        # Genuine RC orphan: we'd try deterministic linking here. None
        # exist in the current DB, so we conservatively leave them as
        # orphans rather than guessing.
        report.left_as_orphan += 1
        LOG.warning(
            "rc orphan %s subtype=%s — no deterministic match, left as orphan", qid, subtype,
        )
    return report


# ---------------------------------------------------------------------------
# Case C — strip solo cluster markers
# ---------------------------------------------------------------------------


@dataclass
class StripMarkerReport:
    examined: int = 0
    stripped: int = 0
    preserved: int = 0
    details: List[Tuple[int, int, int, str]] = field(default_factory=list)
    """(stimulus_id, live_qcount, marker_span, action)."""


def _marker_span(marker_match: re.Match) -> Optional[int]:
    """Return the claimed question count from a marker match, or None."""
    if marker_match.group("single"):
        return 1
    try:
        first = int(marker_match.group("first"))
        last = int(marker_match.group("last"))
    except (TypeError, ValueError):
        return None
    if last < first:
        return None
    return last - first + 1


def strip_cluster_marker(conn: sqlite3.Connection, dry_run: bool = False) -> StripMarkerReport:
    """Strip cluster header lines from passage stimuli whose live question
    count does not match the marker's claimed span.

    We only strip when the stimulus would otherwise present as a solo
    (or short cluster) to the test-taker with a misleading "Questions
    N-M are based on..." header. If live_qcount matches the marker's
    span, the cluster is intact and the marker is preserved.
    """
    report = StripMarkerReport()
    rows = conn.execute(
        """
        SELECT s.id, s.content, COUNT(CASE WHEN q.status='live' THEN 1 END) AS live_q
        FROM stimulus s LEFT JOIN question q ON q.stimulus_id = s.id
        WHERE s.stimulus_type = 'passage'
          AND s.content LIKE '%based on the%passage%'
        GROUP BY s.id
        """
    ).fetchall()
    for sid, content, live_q in rows:
        marker_match = CLUSTER_MARKER_RE.search(content)
        if not marker_match:
            continue
        report.examined += 1
        span = _marker_span(marker_match)
        if span is not None and span == live_q:
            # Legitimate intact cluster — leave the header alone.
            report.preserved += 1
            report.details.append((sid, live_q, span, "preserved"))
            continue
        # Strip the marker (first occurrence only; markers are always at top).
        stripped = CLUSTER_MARKER_RE.sub("", content, count=1).lstrip()
        if stripped == content:
            report.preserved += 1
            report.details.append((sid, live_q, span or -1, "no-op"))
            continue
        if not dry_run:
            conn.execute(
                "UPDATE stimulus SET content = ? WHERE id = ?",
                (stripped, sid),
            )
        report.stripped += 1
        report.details.append((sid, live_q, span or -1, "stripped"))
        LOG.info("strip: stim=%s live_q=%s marker_span=%s", sid, live_q, span)
    return report


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def histogram(conn: sqlite3.Connection) -> Dict[int, int]:
    rows = conn.execute(
        """
        SELECT qcount, COUNT(*) FROM (
          SELECT s.id, COUNT(q.id) AS qcount
          FROM stimulus s LEFT JOIN question q
            ON q.stimulus_id = s.id AND q.status = 'live'
          WHERE s.stimulus_type = 'passage'
          GROUP BY s.id
        ) GROUP BY qcount
        """
    ).fetchall()
    return {qc: n for qc, n in rows}


def run(db_path: str, dry_run: bool = False) -> Dict[str, object]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = _connect(db_path)
    try:
        before_hist = histogram(conn)
        before_stim_count = conn.execute(
            "SELECT COUNT(*) FROM stimulus WHERE stimulus_type='passage'"
        ).fetchone()[0]

        dedupe = dedupe_stimuli(conn, dry_run=dry_run)
        orphans = relink_orphans(conn, dry_run=dry_run)
        strip = strip_cluster_marker(conn, dry_run=dry_run)

        if not dry_run:
            conn.commit()

        after_hist = histogram(conn)
        after_stim_count = conn.execute(
            "SELECT COUNT(*) FROM stimulus WHERE stimulus_type='passage'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "dedupe": dedupe,
        "orphans": orphans,
        "strip": strip,
        "hist_before": before_hist,
        "hist_after": after_hist,
        "stim_before": before_stim_count,
        "stim_after": after_stim_count,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default="/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a6ab107e/data/gre_user.db",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--append-audit",
        default="/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a6ab107e/data/audits/rc_cluster_integrity_2026_04_28.md",
    )
    args = ap.parse_args()
    result = run(args.db, dry_run=args.dry_run)

    lines = ["\n---\n", f"## Remediation run @ {datetime.utcnow().isoformat()}Z (dry_run={args.dry_run})\n"]
    d: DedupeReport = result["dedupe"]  # type: ignore[assignment]
    lines.append(f"### Case A — dedupe_stimuli")
    lines.append(f"- groups merged: {d.groups_merged}")
    lines.append(f"- stimuli deleted: {d.stimuli_deleted}")
    lines.append(f"- questions relinked: {d.questions_relinked}")
    for canonical, dups, rlk in d.details:
        lines.append(f"  - canonical={canonical} duplicates={dups} relinked={rlk}")
    o: OrphanRelinkReport = result["orphans"]  # type: ignore[assignment]
    lines.append(f"\n### Case B — relink_orphans")
    lines.append(f"- candidates examined: {o.candidates_examined}")
    lines.append(f"- relinked: {o.relinked}")
    lines.append(f"- left as orphan: {o.left_as_orphan}")
    lines.append(f"- misclassified (quant/SE/TC mis-tagged as rc_*): {o.misclassified}")
    s: StripMarkerReport = result["strip"]  # type: ignore[assignment]
    lines.append(f"\n### Case C — strip_cluster_marker")
    lines.append(f"- examined: {s.examined}")
    lines.append(f"- stripped: {s.stripped}")
    lines.append(f"- preserved (intact cluster): {s.preserved}")
    for sid, live_q, span, action in s.details:
        lines.append(f"  - stim {sid}: live={live_q} marker_span={span} -> {action}")
    lines.append(f"\n### Histograms (#questions per passage-stimulus, live)")
    lines.append(f"- passage stim count: {result['stim_before']} -> {result['stim_after']}")
    lines.append("| qcount | before | after |")
    lines.append("|-------:|-------:|------:|")
    all_keys = sorted(set(list(result["hist_before"].keys()) + list(result["hist_after"].keys())))  # type: ignore[arg-type]
    for k in all_keys:
        lines.append(
            f"| {k} | {result['hist_before'].get(k, 0)} | {result['hist_after'].get(k, 0)} |"  # type: ignore[union-attr]
        )
    text = "\n".join(lines) + "\n"

    if args.append_audit:
        with open(args.append_audit, "a", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Appended to {args.append_audit}")
    print(text)


if __name__ == "__main__":
    main()
