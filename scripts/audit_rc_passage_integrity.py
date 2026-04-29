"""RC passage integrity audit + repair.

Three phases:

  Step 1 - Deterministic audit
    Walk every live RC passage stimulus, compute heuristic signals of
    truncation (mid-sentence cutoffs, paragraph-count anomalies, gap
    markers, extreme brevity, etc.), and score each on a 0-5 scale.

  Step 2 - LLM deep review (opt-in via --llm)
    For every stimulus with a heuristic score >= 2, prompt Opus 4.7 to
    judge whether the passage is coherent and self-contained. Results
    merged into the audit ledger.

  Step 3 - Repair / retire (opt-in via --apply)
    Passages Opus confirms as incomplete get rechecked against the
    original extraction JSONs (Princeton, Kaplan, Manhattan). If a
    cleaner version exists we replace the stimulus content; otherwise
    the stimulus is marked as retired in render_spec and every linked
    live question is moved to status='retired'. Applied to both gre_user
    and gre_mock databases.

This script does not invent content. If the source JSON is absent or is
itself broken, the item is retired rather than synthesised.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = "/Users/chiku/Documents/side_projects/gre_with_ai"
USER_DB = os.path.join(REPO_ROOT, "data", "gre_user.db")
MOCK_DB = os.path.join(REPO_ROOT, "data", "gre_mock.db")
AUDIT_MD = os.path.join(REPO_ROOT, "data", "audits",
                        "rc_passage_integrity_2026_04_28.md")
AUDIT_JSON = os.path.join(REPO_ROOT, "data", "audits",
                          "rc_passage_integrity_2026_04_28.json")

PRINCETON_JSON = os.path.join(REPO_ROOT, "data", "extracted", "princeton",
                              "princeton_extracted.json")
KAPLAN_DIR = os.path.join(REPO_ROOT, "data", "extracted", "kaplan")


# ──────────────────────────────────────────────────────────────────
# Heuristic detectors
# ──────────────────────────────────────────────────────────────────

SENTENCE_END_RE = re.compile(r"[.!?][\"'”’]?\s*$")
PARAGRAPH_SPLIT_RE = re.compile(r"</p>|\n\s*\n")
ELLIPSIS_MID_RE = re.compile(r"\.\.\.[\s\S]+?[A-Za-z]")
GAP_MARKER_RE = re.compile(
    r"\[\s*(?:\.\.\.|omitted|truncated|snipped|missing|cut)\s*\]|"
    r"\[\s*\.\.\.\s*\]",
    re.IGNORECASE,
)

# Strip simple HTML tags so sentence-end detection looks at real text.
TAG_RE = re.compile(r"<[^>]+>")

# Remove decorative "passage" / "argument" footer badges that sit inside
# styled <p> labels at the very end of the stimulus HTML. Example:
#   <p style="...; color:#a0a0a0; ...">passage</p>
DECORATIVE_LABEL_RE = re.compile(
    r"<(?:p|div)[^>]*(?:a0a0a0|text-align:\s*center)[^>]*>\s*"
    r"(?:passage|argument|stimulus|excerpt)\s*</(?:p|div)>",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    # Drop the decorative trailing label first so it doesn't pollute
    # the "ends abruptly" check.
    cleaned = DECORATIVE_LABEL_RE.sub("", text)
    return TAG_RE.sub("", cleaned).strip()


# Regex for a trailing stray caption left over from figure/title
# extraction: after the last closing </p>, one or more bare <i>...</i>
# or <b>...</b> fragments on their own lines. These are extraction
# artifacts, not real passage content — strip them cleanly.
TRAILING_STRAY_CAPTION_RE = re.compile(
    r"(</p>)(\s*(?:<(?:i|b|em|strong)>[^<>]{1,80}</(?:i|b|em|strong)>\s*)+)\s*$",
    re.IGNORECASE,
)


def strip_trailing_stray_caption(content: str) -> Tuple[str, bool]:
    """Remove trailing orphan <i>/<b> captions after the last </p>.

    Returns (new_content, changed).
    """
    m = TRAILING_STRAY_CAPTION_RE.search(content.rstrip())
    if not m:
        return content, False
    cleaned = content[: m.start()] + m.group(1)
    return cleaned.rstrip(), True


@dataclass
class HeuristicResult:
    stim_id: int
    length: int
    paragraph_count: int
    ends_abruptly: bool
    starts_abruptly: bool
    mid_ellipsis: bool
    gap_marker: bool
    too_short: bool
    suspicion: int = 0
    notes: List[str] = field(default_factory=list)


def count_paragraphs(content: str) -> int:
    # Any </p> or double-newline counts as a break. A 1-paragraph passage
    # still produces 1 chunk, so take max(1, splits+1) semantics:
    if not content.strip():
        return 0
    if "</p>" in content.lower():
        n = len(re.findall(r"</p\s*>", content, re.IGNORECASE))
        return max(n, 1)
    # Fall back to blank-line splitting.
    chunks = [c for c in PARAGRAPH_SPLIT_RE.split(content) if c.strip()]
    return max(len(chunks), 1)


def heuristic_audit(stim_id: int, content: str) -> HeuristicResult:
    plain = strip_html(content)
    r = HeuristicResult(
        stim_id=stim_id,
        length=len(content),
        paragraph_count=count_paragraphs(content),
        ends_abruptly=False,
        starts_abruptly=False,
        mid_ellipsis=False,
        gap_marker=False,
        too_short=False,
    )

    # End-of-passage check uses the last 80 non-tag chars.
    tail = plain[-80:] if plain else ""
    if tail and not SENTENCE_END_RE.search(tail):
        r.ends_abruptly = True
        r.suspicion += 2
        r.notes.append(f"tail={tail[-40:]!r}")

    # Beginning-of-passage: first non-whitespace char should be upper/quote.
    head = plain.lstrip()[:1]
    if head and head.islower():
        r.starts_abruptly = True
        r.suspicion += 1
        r.notes.append(f"head={plain.lstrip()[:40]!r}")

    # Ellipsis mid-content (after the first 40 chars and not near the end).
    # Many real passages legitimately use ellipsis inside a quotation,
    # so this is a weak signal (+1).
    if len(plain) > 80:
        core = plain[40:-40]
        if ELLIPSIS_MID_RE.search(core):
            r.mid_ellipsis = True
            r.suspicion += 1
            r.notes.append("mid-content ellipsis")

    # Gap markers like [omitted], [...]. Strong signal.
    if GAP_MARKER_RE.search(plain):
        r.gap_marker = True
        r.suspicion += 3
        r.notes.append("gap marker token present")

    # Length guard. Very short argument-style stims (~200-400 chars) are
    # legitimate CR prompts, so only flag <200.
    if r.length < 200:
        r.too_short = True
        r.suspicion += 2
        r.notes.append(f"length={r.length}")

    # Paragraph sanity. 1-6 is normal; outside is odd.
    if r.paragraph_count == 0 or r.paragraph_count > 8:
        r.suspicion += 1
        r.notes.append(f"paragraphs={r.paragraph_count}")

    # Clamp to [0, 5].
    r.suspicion = max(0, min(r.suspicion, 5))
    return r


# ──────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_live_rc_passages(db_path: str) -> List[Dict[str, Any]]:
    """Live RC passages only — verbal measure, rc_* or se subtype.

    Passage stimuli bound to quant questions (QC etc.) are stored as the
    same ``stimulus_type='passage'`` row but are not Reading Comp text,
    so we filter them out by joining on the verbal subtypes. Short CR-style
    argument stimuli used by verbal RC (rc_single with short text) are
    retained.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.stimulus_type, s.title, s.content, s.render_spec
            FROM stimulus s
            WHERE s.stimulus_type = 'passage'
              AND s.id IN (
                SELECT stimulus_id FROM question
                WHERE status='live'
                  AND stimulus_id IS NOT NULL
                  AND measure='verbal'
                  AND subtype IN ('rc_single', 'rc_multi', 'se')
              )
            ORDER BY s.id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def questions_for_stim(db_path: str, stim_id: int) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, source, source_anchor, status, subtype, provenance
            FROM question
            WHERE stimulus_id = ? AND status='live'
            """,
            (stim_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────
# Source lookup (Step 3)
# ──────────────────────────────────────────────────────────────────


class SourceIndex:
    """Light-weight content-keyed lookup over the extraction JSONs."""

    def __init__(self) -> None:
        self._princeton: Dict[str, str] = {}
        self._kaplan: Dict[str, str] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        # Princeton
        if os.path.exists(PRINCETON_JSON):
            try:
                with open(PRINCETON_JSON) as fh:
                    data = json.load(fh)
                self._ingest_princeton(data)
            except Exception as exc:
                print(f"[warn] princeton load: {exc}", flush=True)
        # Kaplan
        if os.path.isdir(KAPLAN_DIR):
            for name in os.listdir(KAPLAN_DIR):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(KAPLAN_DIR, name)) as fh:
                        data = json.load(fh)
                    self._ingest_kaplan(data)
                except Exception as exc:
                    print(f"[warn] kaplan load {name}: {exc}", flush=True)

    def _ingest_princeton(self, data: Any) -> None:
        # Extraction JSON is a list of passages / questions; try to index
        # anything that looks like a passage body.
        if isinstance(data, list):
            for entry in data:
                self._maybe_register(entry)
        elif isinstance(data, dict):
            for key in ("passages", "items", "records", "questions"):
                if key in data and isinstance(data[key], list):
                    for entry in data[key]:
                        self._maybe_register(entry)

    def _ingest_kaplan(self, data: Any) -> None:
        self._ingest_princeton(data)  # same shape-agnostic walk

    def _maybe_register(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        anchor = (
            entry.get("source_anchor")
            or entry.get("anchor")
            or entry.get("id")
            or entry.get("key")
        )
        passage = (
            entry.get("passage")
            or entry.get("stimulus")
            or entry.get("body")
            or entry.get("content")
            or entry.get("passage_text")
        )
        if isinstance(passage, dict):
            passage = (
                passage.get("content")
                or passage.get("text")
                or passage.get("body")
            )
        if not anchor or not passage:
            return
        anchor = str(anchor)
        if isinstance(passage, str) and len(passage) > 200:
            # Crude source-routing: the key starts with 'princeton' or
            # contains 'kaplan'. Works because our extraction JSON anchors
            # carry the source prefix. Fall back to Princeton by default.
            low = anchor.lower()
            if "kaplan" in low:
                self._kaplan[anchor] = passage
            else:
                self._princeton[anchor] = passage

    def lookup(self, source: str, anchor: str) -> Optional[str]:
        self.load()
        if not anchor:
            return None
        if source.startswith("princeton"):
            return self._princeton.get(anchor)
        if source.startswith("kaplan"):
            return self._kaplan.get(anchor)
        # Unknown source; try both.
        return self._princeton.get(anchor) or self._kaplan.get(anchor)


# ──────────────────────────────────────────────────────────────────
# LLM deep review (Step 2)
# ──────────────────────────────────────────────────────────────────


OPUS_MODEL = "anthropic.claude-opus-4-7"

LLM_SYSTEM = (
    "You are a veteran GRE editor verifying Reading Comprehension passage "
    "integrity. You look at raw passage text and decide whether it is "
    "coherent and self-contained, or whether paragraphs/sentences are "
    "missing. Respond ONLY with strict JSON matching the schema."
)

LLM_USER_TEMPLATE = """Evaluate this GRE Reading Comprehension passage for integrity.
The passage was extracted from a prep book; we suspect truncation.

Return strict JSON (no prose, no markdown fences) matching:
{{
  "complete": true|false,
  "ends_abruptly": true|false,
  "starts_abruptly": true|false,
  "mid_content_gap": true|false,
  "paragraph_count": <int>,
  "estimated_missing_content": "none"|"small"|"significant",
  "issue_description": "<short explanation or empty string>"
}}

A passage is "complete" if it reads like a self-contained excerpt a GRE
test-taker could answer questions about: it starts with a capital letter
or quotation, ends with terminal punctuation, and has no obvious gaps
between sentences/paragraphs. Trailing source attributions like "(Adapted
from ...)" are fine.

Passage:
<<<
{content}
>>>"""


def llm_review(gateway, content: str, *, stim_id: int,
               max_retries: int = 2) -> Dict[str, Any]:
    """Call Opus 4.7 for a single passage. Returns normalized dict."""
    user = LLM_USER_TEMPLATE.format(content=content[:12000])
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            raw = gateway.call_anthropic(
                model=OPUS_MODEL,
                system=LLM_SYSTEM,
                messages=[{"role": "user", "content": user}],
                max_tokens=500,
                max_retries=3,
            )
            return _parse_llm(raw)
        except Exception as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
    print(f"[llm-err] stim={stim_id}: {last_err}", flush=True)
    return {
        "complete": None,
        "estimated_missing_content": "unknown",
        "error": str(last_err)[:200],
    }


def _parse_llm(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    # Strip code fences.
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines)
    # Look for the first { ... } block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON block in LLM response: {raw[:120]!r}")
    return json.loads(m.group(0))


# ──────────────────────────────────────────────────────────────────
# Audit driver
# ──────────────────────────────────────────────────────────────────


def run_audit(db_path: str) -> List[Dict[str, Any]]:
    passages = fetch_live_rc_passages(db_path)
    out: List[Dict[str, Any]] = []
    for row in passages:
        h = heuristic_audit(row["id"], row["content"])
        entry = asdict(h)
        entry["title"] = row["title"]
        entry["source_hint"] = _source_hint(db_path, row["id"])
        out.append(entry)
    return out


def _source_hint(db_path: str, stim_id: int) -> Dict[str, Any]:
    qs = questions_for_stim(db_path, stim_id)
    if not qs:
        return {"source": None, "anchor": None, "n_live_q": 0}
    # Pick the most common source + anchor among live questions.
    sources = {}
    for q in qs:
        sources.setdefault(q["source"], 0)
        sources[q["source"]] += 1
    top = max(sources, key=sources.get)
    anchors = [q["source_anchor"] for q in qs if q["source"] == top]
    return {
        "source": top,
        "anchor": anchors[0] if anchors else None,
        "n_live_q": len(qs),
    }


def run_llm_phase(audit: List[Dict[str, Any]], *,
                  threshold: int = 2,
                  limit: Optional[int] = None,
                  progress_every: int = 10) -> None:
    candidates = [e for e in audit if e["suspicion"] >= threshold]
    if limit is not None:
        candidates = candidates[:limit]
    print(f"[llm] {len(candidates)} candidates at threshold>={threshold}",
          flush=True)
    if not candidates:
        return

    # Import gateway lazily.
    sys.path.insert(0, REPO_ROOT)
    from services import _llm_gateway as gw  # type: ignore
    client = gw.FloodgateClient()

    # We need full content; reload from user DB in one shot.
    conn = _connect(USER_DB)
    try:
        id_list = [e["stim_id"] for e in candidates]
        qmarks = ",".join("?" for _ in id_list)
        rows = conn.execute(
            f"SELECT id, content FROM stimulus WHERE id IN ({qmarks})",
            id_list,
        ).fetchall()
        contents = {r["id"]: r["content"] for r in rows}
    finally:
        conn.close()

    t0 = time.time()
    for i, entry in enumerate(candidates, 1):
        sid = entry["stim_id"]
        content = contents.get(sid, "")
        review = llm_review(client, content, stim_id=sid)
        entry["llm_review"] = review
        if i % progress_every == 0 or i == len(candidates):
            print(f"[llm] {i}/{len(candidates)} "
                  f"elapsed={time.time()-t0:.1f}s", flush=True)


# ──────────────────────────────────────────────────────────────────
# Repair / retire (Step 3/4)
# ──────────────────────────────────────────────────────────────────


@dataclass
class RepairResult:
    stim_id: int
    action: str   # "repaired" | "retired" | "skipped"
    reason: str
    old_len: int = 0
    new_len: int = 0
    questions_retired: int = 0


def decide_repair(entry: Dict[str, Any]) -> bool:
    """Should this entry be acted on (repair or retire)?"""
    llm = entry.get("llm_review") or {}
    if llm.get("complete") is False:
        return True
    missing = (llm.get("estimated_missing_content") or "").lower()
    if missing in {"small", "significant"}:
        return True
    return False


def apply_repair(audit: List[Dict[str, Any]], *,
                 sources: SourceIndex,
                 db_paths: Sequence[str]) -> List[RepairResult]:
    results: List[RepairResult] = []
    # Pre-read all stimulus contents from the first DB so we can test
    # lightweight in-place cleanups (trailing stray captions) before
    # deciding between source-replace and retire.
    conn = _connect(db_paths[0])
    try:
        contents = {
            r["id"]: r["content"]
            for r in conn.execute(
                "SELECT id, content FROM stimulus"
            ).fetchall()
        }
    finally:
        conn.close()

    for entry in audit:
        if not decide_repair(entry):
            continue
        stim_id = entry["stim_id"]
        hint = entry.get("source_hint") or {}
        source = hint.get("source") or ""
        anchor = hint.get("anchor") or ""
        original = contents.get(stim_id, "")

        # 1. Cheap in-place cleanup: strip trailing stray <i>/<b> captions
        #    that sit outside the last </p>. If the result is coherent by
        #    our heuristic (suspicion < 2), take it.
        cleaned, changed = strip_trailing_stray_caption(original)
        if changed and not _looks_broken(cleaned):
            res = _replace_stim(db_paths, stim_id, cleaned)
            res.reason = "stripped_trailing_stray_caption"
            results.append(res)
            continue

        # 2. Source-JSON lookup.
        repl = sources.lookup(source, anchor) if anchor else None
        if repl and not _looks_broken(repl):
            res = _replace_stim(db_paths, stim_id, repl)
            res.reason = f"source={source} anchor={anchor}"
            results.append(res)
            continue

        # 3. Retire.
        res = _retire_stim(db_paths, stim_id)
        res.reason = "no_source_or_source_broken"
        results.append(res)
    return results


def _looks_broken(text: str) -> bool:
    """Same heuristic, as a quick validator on candidate source text."""
    h = heuristic_audit(0, text)
    return h.suspicion >= 3


def _replace_stim(db_paths: Sequence[str], stim_id: int,
                  new_content: str) -> RepairResult:
    old_len = 0
    for path in db_paths:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT content FROM stimulus WHERE id=?", (stim_id,)
            ).fetchone()
            if row is None:
                continue
            if not old_len:
                old_len = len(row["content"])
            conn.execute(
                "UPDATE stimulus SET content=? WHERE id=?",
                (new_content, stim_id),
            )
            conn.commit()
        finally:
            conn.close()
    return RepairResult(
        stim_id=stim_id, action="repaired", reason="",
        old_len=old_len, new_len=len(new_content),
    )


def _retire_stim(db_paths: Sequence[str], stim_id: int) -> RepairResult:
    qtotal = 0
    for path in db_paths:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT content, render_spec FROM stimulus WHERE id=?",
                (stim_id,),
            ).fetchone()
            if row is None:
                continue
            # Update render_spec with retirement note.
            try:
                spec = json.loads(row["render_spec"] or "{}")
                if not isinstance(spec, dict):
                    spec = {}
            except Exception:
                spec = {}
            spec["retired_reason"] = "incomplete_passage"
            spec["retired_at"] = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE stimulus SET render_spec=? WHERE id=?",
                (json.dumps(spec), stim_id),
            )
            # Retire every live question bound to it.
            cur = conn.execute(
                "UPDATE question SET status='retired', "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE stimulus_id=? AND status='live'",
                (stim_id,),
            )
            qtotal = max(qtotal, cur.rowcount)
            conn.commit()
        finally:
            conn.close()
    return RepairResult(
        stim_id=stim_id, action="retired", reason="",
        old_len=0, new_len=0, questions_retired=qtotal,
    )


# ──────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────


def write_audit(audit: List[Dict[str, Any]],
                repairs: Optional[List[RepairResult]] = None) -> None:
    os.makedirs(os.path.dirname(AUDIT_JSON), exist_ok=True)

    with open(AUDIT_JSON, "w") as fh:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "audit": audit,
                "repairs": [asdict(r) for r in (repairs or [])],
            },
            fh,
            indent=2,
        )

    flagged = [e for e in audit if e["suspicion"] >= 2]
    confirmed = [e for e in audit
                 if (e.get("llm_review") or {}).get("complete") is False
                 or (e.get("llm_review") or {}).get(
                     "estimated_missing_content") in {"small", "significant"}]
    repaired = [r for r in (repairs or []) if r.action == "repaired"]
    retired = [r for r in (repairs or []) if r.action == "retired"]
    q_retired = sum(r.questions_retired for r in retired)

    # Per-source breakdown.
    by_source: Dict[str, Dict[str, int]] = {}
    for e in audit:
        src = (e.get("source_hint") or {}).get("source") or "unknown"
        by_source.setdefault(src, {"total": 0, "flagged": 0,
                                   "confirmed": 0, "retired": 0})
        by_source[src]["total"] += 1
        if e["suspicion"] >= 2:
            by_source[src]["flagged"] += 1
        if ((e.get("llm_review") or {}).get("complete") is False
                or (e.get("llm_review") or {}).get(
                    "estimated_missing_content") in {"small", "significant"}):
            by_source[src]["confirmed"] += 1
    for r in retired:
        for e in audit:
            if e["stim_id"] == r.stim_id:
                src = (e.get("source_hint") or {}).get("source") or "unknown"
                by_source.setdefault(
                    src, {"total": 0, "flagged": 0, "confirmed": 0,
                          "retired": 0})
                by_source[src]["retired"] += 1
                break

    lines: List[str] = []
    lines.append("# RC passage integrity audit — 2026-04-28\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n")
    lines.append("## Scope\n")
    lines.append(f"- Total live RC passages examined: **{len(audit)}**\n")
    lines.append(f"- Flagged by heuristic (suspicion >= 2): "
                 f"**{len(flagged)}**\n")
    lines.append(f"- Confirmed incomplete by Opus 4.7: "
                 f"**{len(confirmed)}**\n")
    lines.append(f"- Repaired from source JSON: **{len(repaired)}**\n")
    lines.append(f"- Retired (irreparable): **{len(retired)}** "
                 f"(with {q_retired} live questions retired)\n\n")

    lines.append("## Per-source breakdown\n")
    lines.append("| Source | Total | Flagged | Confirmed | Retired |\n")
    lines.append("|---|---:|---:|---:|---:|\n")
    for src in sorted(by_source):
        b = by_source[src]
        lines.append(
            f"| {src} | {b['total']} | {b['flagged']} | "
            f"{b['confirmed']} | {b['retired']} |\n"
        )

    lines.append("\n## Flagged items (top 40 by suspicion)\n")
    flagged_sorted = sorted(flagged, key=lambda e: -e["suspicion"])
    for e in flagged_sorted[:40]:
        llm = e.get("llm_review") or {}
        complete = llm.get("complete")
        missing = llm.get("estimated_missing_content")
        lines.append(
            f"- stim **{e['stim_id']}** "
            f"(suspicion={e['suspicion']}, len={e['length']}, "
            f"para={e['paragraph_count']}, "
            f"source={(e.get('source_hint') or {}).get('source')}): "
            f"ends_abrupt={e['ends_abruptly']}, "
            f"starts_abrupt={e['starts_abruptly']}, "
            f"gap={e['gap_marker']}, short={e['too_short']}"
        )
        if complete is not None or missing:
            lines.append(f"  — LLM: complete={complete}, missing={missing}")
        if e.get("notes"):
            lines.append(f"  — notes: {'; '.join(e['notes'])}")
        lines.append("")

    if repairs:
        lines.append("\n## Repair actions\n")
        for r in repairs:
            lines.append(
                f"- stim {r.stim_id}: **{r.action}** "
                f"(old_len={r.old_len}, new_len={r.new_len}, "
                f"questions_retired={r.questions_retired}) — {r.reason}"
            )

    with open(AUDIT_MD, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[audit] wrote {AUDIT_MD} and {AUDIT_JSON}", flush=True)


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--llm", action="store_true",
                   help="Run Opus 4.7 deep review on flagged items")
    p.add_argument("--llm-limit", type=int, default=None,
                   help="Cap number of LLM calls (for smoke tests)")
    p.add_argument("--threshold", type=int, default=2,
                   help="Heuristic suspicion threshold for LLM review")
    p.add_argument("--apply", action="store_true",
                   help="Apply repair/retire writes to both DBs")
    p.add_argument("--audit-only", action="store_true",
                   help="Only produce heuristic audit (Step 1)")
    args = p.parse_args()

    print(f"[audit] fetching live RC passages from {USER_DB}", flush=True)
    audit = run_audit(USER_DB)
    print(f"[audit] {len(audit)} passages scored", flush=True)

    # Stop condition: >20% flagged.
    flagged = [e for e in audit if e["suspicion"] >= args.threshold]
    pct = 100.0 * len(flagged) / max(len(audit), 1)
    print(f"[audit] {len(flagged)} flagged ({pct:.1f}%) at threshold "
          f">={args.threshold}", flush=True)
    if pct > 20.0 and not args.audit_only:
        print("[audit] >20% flagged — surfacing for human review, skipping "
              "LLM + repair phases. Re-run with --audit-only to just dump.",
              flush=True)
        write_audit(audit)
        return 2

    if args.llm and not args.audit_only:
        run_llm_phase(audit, threshold=args.threshold,
                      limit=args.llm_limit)

    repairs: List[RepairResult] = []
    if args.apply:
        sources = SourceIndex()
        repairs = apply_repair(
            audit,
            sources=sources,
            db_paths=[USER_DB, MOCK_DB],
        )

    write_audit(audit, repairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
