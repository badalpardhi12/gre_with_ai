"""Stage E: persist Kaplan extraction outputs into the worktree DB.

Reads ``data/extracted/kaplan/phase0_chapter*.json`` (the per-chapter
deterministic extraction dumps), folds in any per-item LLM verification
verdicts cached at ``data/extracted/kaplan/per_item_verification_cache.json``
or ``full_run_verification_cache.json``, then upserts:

  * ``Stimulus`` rows for RC passages and DI cluster figures.
  * ``Question`` rows with their ``QuestionOption`` and ``NumericAnswer``
    children.
  * Figure assets copied into ``data/extracted/kaplan/assets/`` (relative
    paths persisted in ``Stimulus.render_spec`` JSON).

Idempotency: the script keeps a sidecar ``persistence_index.json`` mapping
``source_ref -> question_id`` and ``stimulus_key -> stimulus_id``. On
re-run, existing rows are updated in place rather than duplicated.

Items that pass every validator gate AND every verification verdict ride
in as ``status='live'``. Items that fail any gate or whose verifier
returned ``verified=false`` (with a non-auto-applicable defect) land as
``status='draft'`` with a JSON dump in the explanation footer for review.

Usage:
    venv/bin/python scripts/persist_kaplan.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Force the persistence to write to the worktree's seed DB
# (data/gre_mock.db) — the runtime gre_user.db is gitignored and per-user,
# so any data we ship with the branch must live in the LFS-tracked seed.
# We patch config.DB_PATH BEFORE importing models.database so the global
# `db = SqliteDatabase(str(DB_PATH))` binds to the right file.
import config  # noqa: E402
SEED_DB = config.SEED_DB_PATH
config.DB_PATH = SEED_DB

from models.database import (  # noqa: E402
    db, init_db, Question, QuestionOption, NumericAnswer, Stimulus,
)
# Belt + braces: rebind the Peewee Database object to the seed DB even
# if config got re-imported elsewhere first.
db.init(str(SEED_DB), pragmas={
    "journal_mode": "wal",
    "cache_size": -1024 * 64,
    "foreign_keys": 1,
    "busy_timeout": 5000,
})

PHASE0_DIR = os.path.join(PROJECT_ROOT, "data", "extracted", "kaplan")
ASSETS_DIR = os.path.join(PHASE0_DIR, "assets")
INDEX_PATH = os.path.join(PHASE0_DIR, "persistence_index.json")
SUMMARY_PATH = os.path.join(PHASE0_DIR, "persistence_summary.json")
VERIFY_CACHE_FILES = (
    "full_run_verification_cache.json",
    "per_item_verification_cache.json",
)
SOURCE_TAG = "kaplan_2024"

# Resolve EPUB path the same way extract_kaplan does (main checkout's
# gitignored data/ebooks/).
def _resolve_epub_path() -> str:
    main_root = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "..", ".."))
    candidates = [
        os.path.join(PROJECT_ROOT, "data", "ebooks"),
        os.path.join(main_root, "data", "ebooks"),
    ]
    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        for f in os.listdir(cand):
            if f.startswith("(Kaplan") and f.endswith(".epub"):
                return os.path.join(cand, f)
    raise FileNotFoundError("Kaplan EPUB not found in data/ebooks/")

EPUB_PATH = _resolve_epub_path()


# ── Index helpers ────────────────────────────────────────────────────

def load_index() -> Dict[str, Any]:
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    return {"questions": {}, "stimuli": {}}


def save_index(idx: Dict[str, Any]) -> None:
    os.makedirs(PHASE0_DIR, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False, sort_keys=True)


# ── Verification cache ───────────────────────────────────────────────

def load_verification_cache() -> Dict[str, Dict[str, Any]]:
    """Return {source_ref: verdict_dict} merged from all cache files.

    Two cache layouts are supported:
      1. The verifier's per-item cache keyed by content-hash; each entry
         is itself a verdict dict but doesn't carry the source_ref.
      2. The bulk-run cache keyed directly by source_ref.

    For (1) the verdicts are unkeyed at the source-ref level; we ignore
    them — re-render uses (2) when present.
    """
    out: Dict[str, Dict[str, Any]] = {}
    full_cache = os.path.join(PHASE0_DIR, VERIFY_CACHE_FILES[0])
    if os.path.exists(full_cache):
        with open(full_cache) as f:
            blob = json.load(f)
        # bulk-run shape: {chapter_id: {verdicts: [{qst_id: ..., verified: ...}]}}
        if isinstance(blob, dict):
            for ch_id, payload in blob.items():
                verdicts = payload.get("verdicts", []) if isinstance(payload, dict) else []
                for v in verdicts:
                    qid = v.get("qst_id")
                    if qid:
                        out[qid] = v
    return out


# ── Validation snapshot ──────────────────────────────────────────────

def _validate_item(item: Dict[str, Any], block: Dict[str, Any]) -> List[Dict[str, Any]]:
    from validators.kaplan import validate
    issues = validate(item)
    return issues


def _has_block_failure(issues: List[Dict[str, Any]]) -> bool:
    return any(i.get("severity") == "block" for i in issues)


# ── Subtype mapping ──────────────────────────────────────────────────

# Map the parser's subtype to the Question.subtype + Question.measure.
# Parser values: tc, se, qc, mcq_single, mcq_multi, mcq_short_answer,
# numeric_entry, rc_single, rc_multi, rc_select_passage, data_interp.
_SUBTYPE_PASSTHROUGH = {
    "tc", "se", "qc", "mcq_single", "mcq_multi", "numeric_entry",
    "rc_single", "rc_multi", "rc_select_passage", "data_interp",
}


def _resolve_subtype(item: Dict[str, Any]) -> str:
    sub = item.get("subtype") or "mcq_single"
    if sub == "mcq_short_answer":
        # Schema doesn't have this; treat as numeric_entry for routing,
        # since it's a free-response. Parser populates correct_label with
        # the printed answer text.
        return "numeric_entry"
    if sub in _SUBTYPE_PASSTHROUGH:
        return sub
    return "mcq_single"


def _resolve_measure(item: Dict[str, Any]) -> str:
    m = item.get("measure") or "verbal"
    if m in ("verbal", "quant", "awa"):
        return m
    return "verbal"


def _resolve_difficulty(item: Dict[str, Any]) -> int:
    band = (item.get("difficulty_band") or "").lower()
    return {
        "basic": 2, "easy": 2,
        "intermediate": 3, "medium": 3,
        "advanced": 4, "hard": 4,
    }.get(band, 3)


def _option_correct_set(item: Dict[str, Any]) -> set:
    """Compute the set of correct labels — trust the parser's per-option
    is_correct flags first; fall back to splitting correct_label."""
    out = set()
    for o in item.get("options") or []:
        if o.get("is_correct"):
            out.add(o.get("label"))
    if out:
        return out
    raw = (item.get("correct_label") or "").strip()
    for tok in re.split(r"[,\s/]+", raw):
        tok = tok.strip()
        if tok:
            out.add(tok)
    return out


# ── Stimulus persistence ─────────────────────────────────────────────

def _stimulus_key_for_rc(chapter_id: str, set_index: int,
                         q_start: int, q_end: int) -> str:
    return f"{chapter_id}:set{set_index}:rc{q_start}-{q_end}"


def _copy_asset(epub: zipfile.ZipFile, src_filename: str) -> Optional[str]:
    """Copy an EPUB image to assets/ and return a project-relative path."""
    if not src_filename:
        return None
    name = src_filename.rsplit("/", 1)[-1]
    candidates = [f"OEBPS/images/{name}", f"OEBPS/{name}", name]
    for c in candidates:
        try:
            blob = epub.read(c)
            break
        except KeyError:
            continue
    else:
        return None
    os.makedirs(ASSETS_DIR, exist_ok=True)
    dest = os.path.join(ASSETS_DIR, name)
    if not os.path.exists(dest) or os.path.getsize(dest) != len(blob):
        with open(dest, "wb") as f:
            f.write(blob)
    rel = os.path.relpath(dest, PROJECT_ROOT)
    return rel


def upsert_stimulus_for_rc(group: Dict[str, Any],
                           chapter_id: str, set_index: int,
                           epub: zipfile.ZipFile,
                           idx: Dict[str, Any]) -> int:
    """Create / update a Stimulus row for an RC or DI cluster. Returns id."""
    q_start = group.get("q_start")
    q_end = group.get("q_end") or q_start
    key = _stimulus_key_for_rc(chapter_id, set_index, q_start, q_end)
    kind = group.get("kind") or "passage"
    stim_type = "passage"
    if kind in ("graph", "chart"):
        stim_type = "graph"
    elif kind in ("table",):
        stim_type = "table"

    asset_paths = []
    for fig in group.get("figure_images") or []:
        rel = _copy_asset(epub, fig)
        if rel:
            asset_paths.append(rel)
    render_spec = json.dumps({
        "source": SOURCE_TAG,
        "chapter_id": chapter_id,
        "set_index": set_index,
        "q_start": q_start,
        "q_end": q_end,
        "image_paths": asset_paths,
        "kind": kind,
    }, ensure_ascii=False)
    title = f"{chapter_id} q{q_start}-{q_end}"
    body = group.get("passage_html") or ""

    existing_id = idx["stimuli"].get(key)
    if existing_id:
        try:
            row = Stimulus.get_by_id(existing_id)
        except Stimulus.DoesNotExist:
            row = None
        if row is not None:
            row.stimulus_type = stim_type
            row.title = title
            row.content = body
            row.render_spec = render_spec
            row.save()
            return row.id
    row = Stimulus.create(
        stimulus_type=stim_type,
        title=title,
        content=body,
        render_spec=render_spec,
    )
    idx["stimuli"][key] = row.id
    return row.id


# ── Question persistence ─────────────────────────────────────────────

def _build_concept_tags(item: Dict[str, Any], block: Dict[str, Any],
                        chapter_id: str) -> List[str]:
    tags = [
        SOURCE_TAG,
        chapter_id,
        f"section:{block.get('section_title','').strip()}" if block.get('section_title') else None,
        f"subtype:{item.get('subtype')}",
    ]
    if item.get("difficulty_band"):
        tags.append(f"band:{item['difficulty_band']}")
    if item.get("rc_group_key"):
        tags.append(f"rc_group:{item['rc_group_key'][0]}-{item['rc_group_key'][1]}")
    return [t for t in tags if t]


def _explanation_with_review_notes(item: Dict[str, Any],
                                   verdict: Optional[Dict[str, Any]],
                                   gate_issues: List[Dict[str, Any]]) -> str:
    body = item.get("explanation") or ""
    notes = []
    if verdict and not verdict.get("verified"):
        notes.append({
            "kind": "verifier",
            "defects": verdict.get("defects", []),
            "suggested_correction": verdict.get("suggested_correction"),
        })
    if gate_issues:
        notes.append({
            "kind": "validators",
            "issues": [{"severity": i.get("severity"), "kind": i.get("kind")}
                       for i in gate_issues],
        })
    if notes:
        body += "\n\n<!-- review_notes:\n"
        body += json.dumps(notes, indent=2, ensure_ascii=False)
        body += "\n-->\n"
    return body


def upsert_question(item: Dict[str, Any], block: Dict[str, Any],
                    chapter_id: str, *,
                    stimulus_id: Optional[int],
                    epub: zipfile.ZipFile,
                    idx: Dict[str, Any],
                    verdict_lookup: Dict[str, Dict[str, Any]],
                    ) -> Tuple[int, str]:
    """Upsert a Question + options + numeric answer. Returns (qid, status)."""
    source_ref = item.get("source_ref") or ""
    if not source_ref:
        raise ValueError(f"item missing source_ref: chapter={chapter_id} q={item.get('q_number')}")

    gate_issues = _validate_item(item, block)
    verdict = verdict_lookup.get(source_ref)
    fail_reason = None
    if _has_block_failure(gate_issues):
        fail_reason = "validator_block"
    elif verdict and not verdict.get("verified") and not verdict.get("skipped"):
        # Verifier disagreement → draft.
        fail_reason = "verifier_unverified"
    status = "draft" if fail_reason else "live"

    measure = _resolve_measure(item)
    subtype = _resolve_subtype(item)
    prompt = item.get("prompt") or ""
    difficulty = _resolve_difficulty(item)
    explanation = _explanation_with_review_notes(item, verdict, gate_issues)
    concept_tags = _build_concept_tags(item, block, chapter_id)
    # Stash the source_ref + chapter context inside concept_tags so the
    # persistence is self-describing even without the sidecar index.
    concept_tags = [f"source_ref:{source_ref}"] + concept_tags

    existing_qid = idx["questions"].get(source_ref)
    if existing_qid:
        try:
            q = Question.get_by_id(existing_qid)
        except Question.DoesNotExist:
            q = None
    else:
        q = None

    if q is None:
        q = Question.create(
            measure=measure, subtype=subtype,
            stimulus=stimulus_id,
            prompt=prompt,
            difficulty_target=difficulty,
            time_target_seconds=90 if measure == "verbal" else 105,
            concept_tags=json.dumps(concept_tags),
            topic=chapter_id,
            subtopic=(block.get("section_title") or "").strip().lower().replace(" ", "_")[:128],
            question_type=item.get("subtype") or "",
            source=SOURCE_TAG,
            provenance="imported",
            status=status,
            explanation=explanation,
        )
        idx["questions"][source_ref] = q.id
    else:
        q.measure = measure
        q.subtype = subtype
        q.stimulus = stimulus_id
        q.prompt = prompt
        q.difficulty_target = difficulty
        q.concept_tags = json.dumps(concept_tags)
        q.topic = chapter_id
        q.subtopic = (block.get("section_title") or "").strip().lower().replace(" ", "_")[:128]
        q.question_type = item.get("subtype") or ""
        q.source = SOURCE_TAG
        q.status = status
        q.explanation = explanation
        q.save()

    # Refresh options & numeric answers (delete + recreate is simplest
    # and safe inside a transaction).
    QuestionOption.delete().where(QuestionOption.question == q).execute()
    NumericAnswer.delete().where(NumericAnswer.question == q).execute()

    correct_set = _option_correct_set(item)
    seen_labels = set()
    for o in item.get("options") or []:
        label = o.get("label") or ""
        # Defensive: the parser has been observed to emit duplicate labels
        # for SE/TC items where the publisher used merged option columns
        # (chapter08:set1:q8/q12). The DB schema enforces a unique
        # (question_id, label) tuple, so disambiguate by suffixing while
        # preserving the original label in the option text. The verifier
        # has already routed these to draft so this is only to keep the
        # row insertable for human review.
        original_label = label
        suffix = 2
        while label in seen_labels:
            label = f"{original_label}_dup{suffix}"
            suffix += 1
        seen_labels.add(label)
        QuestionOption.create(
            question=q,
            option_label=label,
            option_text=o.get("text") or "",
            is_correct=bool(o.get("is_correct") or (original_label in correct_set)),
        )

    if subtype == "numeric_entry":
        # Parser stores either the printed answer (mcq_short_answer) or a
        # numeric string. Try to parse a float; if it's a fraction,
        # split; otherwise stash as exact_value=NaN-equivalent (skip).
        raw = (item.get("numeric_value") or item.get("correct_label") or "").strip()
        num, den, val = _parse_numeric(raw)
        if val is not None or (num is not None and den is not None):
            NumericAnswer.create(
                question=q,
                exact_value=val,
                numerator=num,
                denominator=den,
                tolerance=0.001 if val is not None else 0.0,
                mode="fraction" if (num is not None and den is not None) else "decimal",
            )

    return q.id, status


# ── Numeric parsing ──────────────────────────────────────────────────

_FRACTION_RE = re.compile(r"^\s*(-?\d+)\s*/\s*(\d+)\s*$")
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _parse_numeric(raw: str) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Return (numerator, denominator, exact_value). Only one shape is
    populated; the other is None. Returns (None, None, None) for things
    we can't parse (e.g. coordinate tuples, symbolic expressions)."""
    if not raw:
        return (None, None, None)
    s = raw.strip().strip(".").replace(",", "")
    # Normalise Unicode minus / dashes to ASCII hyphen so the regex matches.
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    m = _FRACTION_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), None)
    if _NUMERIC_RE.match(s):
        try:
            return (None, None, float(s))
        except ValueError:
            return (None, None, None)
    # Strip a leading $ for currency.
    if s.startswith("$") and _NUMERIC_RE.match(s[1:]):
        return (None, None, float(s[1:]))
    return (None, None, None)


# ── Driver ───────────────────────────────────────────────────────────

def persist_chapter(chapter_path: str, *,
                    epub: zipfile.ZipFile,
                    idx: Dict[str, Any],
                    verdict_lookup: Dict[str, Dict[str, Any]],
                    dry_run: bool) -> Dict[str, int]:
    with open(chapter_path) as f:
        dump = json.load(f)
    chapter_id = dump.get("chapter_id") or os.path.basename(chapter_path)
    counts = {"live": 0, "draft": 0, "stimuli": 0}
    if dry_run:
        ctx = _NullCtx()
    else:
        ctx = db.atomic()

    with ctx:
        for block in dump.get("blocks", []):
            set_index = block.get("set_index", 1)
            # Build per-cluster stimulus first so questions can FK them.
            cluster_to_sid: Dict[Tuple[int, int], int] = {}
            for grp in block.get("rc_groups", []):
                if dry_run:
                    cluster_to_sid[(grp["q_start"], grp.get("q_end") or grp["q_start"])] = -1
                    counts["stimuli"] += 1
                    continue
                sid = upsert_stimulus_for_rc(grp, chapter_id, set_index, epub, idx)
                cluster_to_sid[(grp["q_start"], grp.get("q_end") or grp["q_start"])] = sid
                counts["stimuli"] += 1

            for item in block.get("items", []):
                stim_id = None
                if item.get("rc_group_key"):
                    key = tuple(item["rc_group_key"])
                    stim_id = cluster_to_sid.get(key)
                if dry_run:
                    gate_issues = _validate_item(item, block)
                    verdict = verdict_lookup.get(item.get("source_ref",""))
                    if _has_block_failure(gate_issues):
                        st = "draft"
                    elif verdict and not verdict.get("verified") and not verdict.get("skipped"):
                        st = "draft"
                    else:
                        st = "live"
                    counts[st] += 1
                    continue
                _, status = upsert_question(
                    item, block, chapter_id,
                    stimulus_id=stim_id,
                    epub=epub, idx=idx,
                    verdict_lookup=verdict_lookup,
                )
                counts[status] += 1
    return counts


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + classify but don't write to DB")
    ap.add_argument("--chapters", default=None,
                    help="comma-separated chapter ids (default: all phase0_chapter*.json)")
    args = ap.parse_args()

    if not args.dry_run:
        init_db()
        db.connect(reuse_if_open=True)

    idx = load_index()
    verdict_lookup = load_verification_cache()
    epub = zipfile.ZipFile(EPUB_PATH)

    chapter_files: List[str] = []
    if args.chapters:
        for tok in args.chapters.split(","):
            tok = tok.strip()
            if tok:
                p = os.path.join(PHASE0_DIR, f"phase0_{tok}.json")
                if os.path.exists(p):
                    chapter_files.append(p)
    else:
        for fn in sorted(os.listdir(PHASE0_DIR)):
            if re.match(r"phase0_chapter\d{2}\.json$", fn):
                chapter_files.append(os.path.join(PHASE0_DIR, fn))

    summary: Dict[str, Any] = {"chapters": {}, "totals": {"live": 0, "draft": 0, "stimuli": 0}}
    for cp in chapter_files:
        chapter_id = re.search(r"phase0_(chapter\d{2})", cp).group(1)
        counts = persist_chapter(cp, epub=epub, idx=idx,
                                 verdict_lookup=verdict_lookup,
                                 dry_run=args.dry_run)
        summary["chapters"][chapter_id] = counts
        for k, v in counts.items():
            summary["totals"][k] += v
        print(f"  {chapter_id}: live={counts['live']} draft={counts['draft']} "
              f"stimuli={counts['stimuli']}")

    if not args.dry_run:
        save_index(idx)
        with open(SUMMARY_PATH, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"\nIndex -> {INDEX_PATH}")
        print(f"Summary -> {SUMMARY_PATH}")
    print(f"\nTotals: live={summary['totals']['live']} "
          f"draft={summary['totals']['draft']} "
          f"stimuli={summary['totals']['stimuli']}")
    epub.close()


if __name__ == "__main__":
    main()
