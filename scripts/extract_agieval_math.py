#!/usr/bin/env python3
"""
AGIEval LSAT + Hendrycks MATH extraction + GRE-style reformat pipeline
(Phase 4 · D4).

SOURCES
    AGIEval LSAT-LR  — LSAT Logical Reasoning     -> GRE verbal / text_completion
    AGIEval LSAT-RC  — LSAT Reading Comprehension -> GRE verbal / rc_single|rc_multi
    Hendrycks MATH   — AMC/AIME-style competition -> GRE quant / mcq_single|numeric_entry

    LSAT-LR ~ 500 items; LSAT-RC ~ 260 items; MATH ~ 12.5K items. We
    filter MATH to Levels 1-3 (~1.5-2K items) because Level 4-5 run
    above GRE difficulty.

LICENSING
    AGIEval is MIT (Microsoft); MATH is MIT (Hendrycks et al., arxiv
    2103.03874). Research use only. See Phase 4 open-question #3.

SUBTYPE MAPPING
    agieval_lsat_rc     → rc_single  if only 1 question shares the passage
                          rc_multi   if the passage has ≥ 2 questions
                                     (LSAT-RC passages carry 5-8 questions
                                      each; most items land as rc_multi)
    agieval_lsat_lr     → text_completion
                          (LR has no real GRE analogue; TC is the closest
                          verbal-reasoning shape)
    hendrycks_math_L*   → mcq_single    if the item has 4-5 options OR was
                                         reformatted with MCQ options
                          numeric_entry if the raw answer is numeric and
                                         we kept it free-response

REFORMAT
    --reformat sends each raw item to services.llm_service.llm_service
    with a prompt that asks Opus 4.7 to restate the problem in GRE
    register. Expected JSON shape:

        {
          "prompt":          "...",
          "options":         [{"label": "A", "text": "..."}, ...],   # MCQ
          "correct_answer":  "A"   |  "42"   |  [...],               # MCQ label or numeric string
          "explanation":     "..."
        }

    The LLM is NEVER hit by the test suite: tests monkeypatch
    ``llm_service.generate_json`` and the dataset loader, so no network
    I/O happens in CI.

DATA FETCH
    We do not require the ``datasets`` HuggingFace library (it pulls in
    PyArrow + ~pytorch). Instead, we use direct HTTPS GETs against the
    HuggingFace parquet mirror (``resolve/main/...parquet``) via the
    ``requests`` library (already a project dep). If ``datasets`` *is*
    installed, we prefer it — it handles HF auth + streaming better.

USAGE
    # Dry-run — touch nothing on disk, no DB writes, no LLM
    venv/bin/python scripts/extract_agieval_math.py \\
        --source math --max-items 5 --dry-run

    # Ingest LSAT-RC as-is (no reformat)
    venv/bin/python scripts/extract_agieval_math.py --source agieval

    # Full reformat pass on MATH, 500 items, real DB writes
    venv/bin/python scripts/extract_agieval_math.py \\
        --source math --max-items 500 --reformat

QUALITY CHECK
    Items land with status='candidate'. Pull a random sample:

        sqlite> SELECT id, prompt FROM question
                WHERE source LIKE 'hendrycks_math_%'
                  AND status = 'candidate'
                ORDER BY RANDOM() LIMIT 5;

    Then spot-check the reformatted prompt vs the raw provenance blob
    stored in ``provenance_json`` (key ``raw``).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger("extract_agieval_math")


# ── Dataset shard URLs (direct parquet fallback) ─────────────────────
#
# We keep these as module-level so tests can monkeypatch them. When
# ``datasets`` isn't installed we GET one parquet shard per subset.
# HuggingFace's LFS mirror URL pattern:
#     https://huggingface.co/datasets/<repo>/resolve/main/<path>
#
# These URLs are reference-only — the CI test suite never hits them.
# An operator running the script with --fetch (default on) needs network
# and enough disk to cache the parquet locally.

HF_SHARDS: Dict[str, List[str]] = {
    "agieval_lsat_ar": [
        # AGIEval ar = analytical reasoning — not used, but we keep the
        # shard list explicit so callers can extend later.
    ],
    "agieval_lsat_lr": [
        "https://huggingface.co/datasets/lighteval/agi_eval_en/resolve/main/data/lsat_lr/test-00000-of-00001.parquet",
    ],
    "agieval_lsat_rc": [
        "https://huggingface.co/datasets/lighteval/agi_eval_en/resolve/main/data/lsat_rc/test-00000-of-00001.parquet",
    ],
    "hendrycks_math": [
        # MATH ships as 7 category subsets (algebra, counting_and_probability,
        # geometry, intermediate_algebra, number_theory, prealgebra, precalculus)
        # each with train/test splits. We pull the test split across all
        # categories. Operators who want the full 12.5K can add train.
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/algebra/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/counting_and_probability/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/geometry/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/intermediate_algebra/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/number_theory/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/prealgebra/test-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hendrycks/competition_math/resolve/main/data/precalculus/test-00000-of-00001.parquet",
    ],
}

CACHE_DIR = PROJECT_ROOT / "data" / "external" / "agieval_math"


# ── Raw item shape ────────────────────────────────────────────────────

@dataclass
class RawItem:
    """Canonical pre-reformat record produced by the dataset loaders.

    Every source is normalized to this shape before the reformat stage,
    so the LLM prompt + upsert code don't branch on origin.
    """
    source: str                          # e.g. "agieval_lsat_rc"
    anchor: str                          # stable per-item id, e.g. "rc_p03_q02"
    measure: str                         # "verbal" | "quant"
    subtype: str                         # rc_single | rc_multi | text_completion | mcq_single | numeric_entry
    prompt: str                          # full item text (passage + stem if RC)
    passage: str = ""                    # RC passage, blank for non-RC
    options: List[Tuple[str, str]] = field(default_factory=list)   # [(label, text)]
    correct_answer: str = ""             # MCQ label OR numeric string
    explanation: str = ""                # optional
    difficulty_level: Optional[int] = None  # MATH: 1-5; LSAT: None
    raw: Dict[str, Any] = field(default_factory=dict)  # untouched source row, for provenance


# ── Dataset loaders ───────────────────────────────────────────────────

def _try_load_datasets(repo: str, subset: Optional[str] = None) -> Optional[Iterable[Dict[str, Any]]]:
    """Use the ``datasets`` library if available, else return None.

    Called for each subset. Returns a list of dicts on success.
    """
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        return None
    try:
        ds = load_dataset(repo, subset) if subset else load_dataset(repo)
        # Prefer the test split when present.
        split = "test" if "test" in ds else next(iter(ds.keys()))
        return list(ds[split])
    except Exception as exc:  # pragma: no cover — network path
        logger.warning("datasets.load_dataset(%s, %s) failed: %s", repo, subset, exc)
        return None


def _try_load_parquet(urls: List[str], cache_dir: Path) -> Optional[List[Dict[str, Any]]]:
    """Fallback: GET each parquet shard, read via pyarrow if present.

    Returns a flat list of row-dicts across all shards, or None if the
    pipeline is unavailable (no pyarrow / no network).
    """
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        logger.info("pyarrow not available — skipping parquet fallback")
        return None

    try:
        import requests
    except ImportError:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for url in urls:
        shard_name = url.rsplit("/", 2)
        fname = "_".join(shard_name[-2:])
        dest = cache_dir / fname
        if not dest.exists() or dest.stat().st_size == 0:
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
            except Exception as exc:  # pragma: no cover — network path
                logger.warning("parquet fetch failed for %s: %s", url, exc)
                continue
        try:
            table = pq.read_table(dest)
            rows.extend(table.to_pylist())
        except Exception as exc:  # pragma: no cover
            logger.warning("parquet read failed for %s: %s", dest, exc)
            continue
    return rows


# Test seam: if a test monkeypatches this to return a list, the script
# skips all network logic and uses the injected fixture. Default None
# = "really load from HF".
_INJECTED_LOADER = None


def load_raw_rows(source: str) -> List[Dict[str, Any]]:
    """Return the raw row list for a given source key.

    source ∈ {"agieval_lsat_lr", "agieval_lsat_rc", "hendrycks_math"}

    Priority order:
      1. Monkeypatched ``_INJECTED_LOADER`` (tests).
      2. ``datasets`` library.
      3. Direct parquet GET + pyarrow.
      4. Empty list + warning.
    """
    if _INJECTED_LOADER is not None:
        return list(_INJECTED_LOADER(source))

    # Try datasets library first.
    if source.startswith("agieval_lsat_"):
        subset = source.replace("agieval_", "")  # "lsat_lr" or "lsat_rc"
        rows = _try_load_datasets("lighteval/agi_eval_en", subset)
    elif source == "hendrycks_math":
        rows = _try_load_datasets("hendrycks/competition_math", None)
    else:
        raise ValueError(f"unknown source: {source}")

    if rows is not None:
        return rows

    # Parquet fallback.
    urls = HF_SHARDS.get(source, [])
    if not urls:
        logger.warning("no shard URLs configured for source=%s", source)
        return []
    rows = _try_load_parquet(urls, CACHE_DIR / source)
    return rows or []


# ── Normalizers ────────────────────────────────────────────────────────
#
# Each source gets a function that takes the list of raw row dicts and
# yields RawItem records. Schemas per source (as they appear in HF):
#
# AGIEval LSAT (both LR + RC): common fields
#     query / passage / options (list[str]) / gold / explanation (maybe)
#   For RC, many rows share the same `passage` — we bucket by passage
#   to decide rc_single vs rc_multi.
#
# Hendrycks MATH:
#     problem / level ("Level 3") / type (e.g. "Algebra") / solution
#   No pre-supplied options; we either LLM-reformat to MCQ or keep as
#   numeric_entry using the boxed answer from `solution`.


_LSAT_LABELS = ["A", "B", "C", "D", "E"]


def _extract_gold_label(gold: Any) -> Optional[str]:
    """AGIEval `gold` may be a list, a string index, or a letter. Normalize."""
    if isinstance(gold, list) and gold:
        gold = gold[0]
    if gold is None or gold == "":
        return None
    s = str(gold).strip()
    # Already a letter?
    if len(s) == 1 and s.upper() in _LSAT_LABELS:
        return s.upper()
    # Numeric index? Support "0"..."4" or "1"..."5".
    if s.isdigit():
        idx = int(s)
        if 0 <= idx < len(_LSAT_LABELS):
            return _LSAT_LABELS[idx]
        if 1 <= idx <= len(_LSAT_LABELS):
            return _LSAT_LABELS[idx - 1]
    return None


def normalize_agieval_lsat_lr(rows: List[Dict[str, Any]]) -> List[RawItem]:
    """LSAT-LR → text_completion tag."""
    out: List[RawItem] = []
    for i, row in enumerate(rows):
        query = row.get("query") or row.get("question") or row.get("problem") or ""
        opts = row.get("options") or row.get("choices") or []
        label = _extract_gold_label(row.get("gold") or row.get("label") or row.get("answer"))
        if not query or not opts or not label:
            continue
        options = [(_LSAT_LABELS[j], str(opts[j]).strip())
                   for j in range(min(len(opts), len(_LSAT_LABELS)))]
        out.append(RawItem(
            source="agieval_lsat_lr",
            anchor=f"lr_{i:04d}",
            measure="verbal",
            subtype="text_completion",
            prompt=query.strip(),
            options=options,
            correct_answer=label,
            explanation=(row.get("explanation") or "").strip(),
            raw=row,
        ))
    return out


def normalize_agieval_lsat_rc(rows: List[Dict[str, Any]]) -> List[RawItem]:
    """LSAT-RC → rc_single / rc_multi based on per-passage question count."""
    # Bucket by passage text so we can decide subtype.
    by_passage: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for i, row in enumerate(rows):
        passage = (row.get("passage") or row.get("context") or "").strip()
        by_passage.setdefault(passage, []).append((i, row))

    out: List[RawItem] = []
    for p_idx, (passage, bucket) in enumerate(by_passage.items()):
        subtype = "rc_multi" if len(bucket) >= 2 else "rc_single"
        for q_idx, (orig_i, row) in enumerate(bucket):
            query = row.get("query") or row.get("question") or ""
            opts = row.get("options") or row.get("choices") or []
            label = _extract_gold_label(row.get("gold") or row.get("label") or row.get("answer"))
            if not query or not opts or not label:
                continue
            options = [(_LSAT_LABELS[j], str(opts[j]).strip())
                       for j in range(min(len(opts), len(_LSAT_LABELS)))]
            out.append(RawItem(
                source="agieval_lsat_rc",
                anchor=f"rc_p{p_idx:03d}_q{q_idx:02d}",
                measure="verbal",
                subtype=subtype,
                prompt=query.strip(),
                passage=passage,
                options=options,
                correct_answer=label,
                explanation=(row.get("explanation") or "").strip(),
                raw=row,
            ))
    return out


_MATH_LEVEL_RE = re.compile(r"Level\s*(\d)", re.IGNORECASE)
_MATH_BOXED_RE = re.compile(r"\\boxed\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")


def _math_level(raw_level: Any) -> Optional[int]:
    if raw_level is None:
        return None
    if isinstance(raw_level, int):
        return raw_level
    m = _MATH_LEVEL_RE.search(str(raw_level))
    return int(m.group(1)) if m else None


def _math_boxed_answer(solution: str) -> str:
    """Pull the \\boxed{...} final answer out of the MATH solution text."""
    if not solution:
        return ""
    m = _MATH_BOXED_RE.search(solution)
    return m.group(1).strip() if m else ""


def normalize_hendrycks_math(rows: List[Dict[str, Any]],
                             max_level: int = 3) -> List[RawItem]:
    """MATH → numeric_entry by default; filter to Level ≤ max_level."""
    out: List[RawItem] = []
    for i, row in enumerate(rows):
        level = _math_level(row.get("level"))
        if level is None or level > max_level:
            continue
        problem = (row.get("problem") or "").strip()
        solution = (row.get("solution") or "").strip()
        if not problem:
            continue
        answer = _math_boxed_answer(solution)
        category = (row.get("type") or "unknown").lower().replace(" ", "_")
        out.append(RawItem(
            source=f"hendrycks_math_L{level}",
            anchor=f"{category}_{i:05d}",
            measure="quant",
            subtype="numeric_entry",
            prompt=problem,
            options=[],
            correct_answer=answer,
            explanation=solution,
            difficulty_level=level,
            raw=row,
        ))
    return out


# ── LLM reformat layer ─────────────────────────────────────────────────

_REFORMAT_SYSTEM_PROMPT = """You are a GRE content editor. You convert LSAT or \
competition-math problems into GRE-register items. Rules:

1. Preserve the mathematical content and the logical structure EXACTLY —
   if the raw item has a unique numeric answer, the reformatted item
   must have the same numeric answer.
2. Adjust only the phrasing/register so the item sounds like a GRE
   problem (no LSAT-specific jargon like "plausible inference"; no
   contest-math cultural references).
3. For MCQ problems return 5 options (A-E). For numeric problems keep
   no options.
4. Return a JSON object with keys: prompt, options, correct_answer,
   explanation. `options` is a list of {label, text} dicts (may be
   empty). `correct_answer` is the option label for MCQ or the numeric
   answer (as a string) for free-response. `explanation` is a 1-3
   sentence solution trace.
5. Output JSON only, no markdown fences.
"""


def _reformat_one(item: RawItem, llm) -> Optional[Dict[str, Any]]:
    """Send one RawItem to the LLM and return the parsed JSON, or None on error."""
    user_parts: List[str] = []
    if item.passage:
        user_parts.append(f"PASSAGE:\n{item.passage}")
    user_parts.append(f"PROMPT:\n{item.prompt}")
    if item.options:
        opts_str = "\n".join(f"  ({lbl}) {t}" for lbl, t in item.options)
        user_parts.append(f"OPTIONS:\n{opts_str}")
    if item.correct_answer:
        user_parts.append(f"CORRECT_ANSWER: {item.correct_answer}")
    if item.explanation:
        user_parts.append(f"RAW_EXPLANATION:\n{item.explanation}")
    user_parts.append(f"SOURCE: {item.source}  SUBTYPE: {item.subtype}")

    user_prompt = "\n\n".join(user_parts)
    try:
        return llm.generate_json(_REFORMAT_SYSTEM_PROMPT, user_prompt)
    except Exception as exc:
        logger.warning("reformat failed for %s/%s: %s", item.source, item.anchor, exc)
        return None


def apply_reformat(items: List[RawItem], llm) -> Tuple[List[RawItem], int]:
    """Replace each item's prompt/options/correct_answer/explanation with
    the LLM-reformatted version. Items the LLM fails on are dropped.

    Returns (reformatted_items, n_llm_calls).
    """
    out: List[RawItem] = []
    calls = 0
    for item in items:
        calls += 1
        resp = _reformat_one(item, llm)
        if not resp:
            continue
        new_prompt = (resp.get("prompt") or "").strip()
        if not new_prompt:
            continue
        raw_opts = resp.get("options") or []
        new_opts: List[Tuple[str, str]] = []
        for o in raw_opts:
            if isinstance(o, dict):
                lbl = str(o.get("label", "")).strip()
                txt = str(o.get("text", "")).strip()
                if lbl and txt:
                    new_opts.append((lbl, txt))
        new_answer = str(resp.get("correct_answer", "")).strip()
        new_explanation = (resp.get("explanation") or "").strip()

        # If the reformatter produced options, the item becomes mcq_single
        # (unless it was already RC). Numeric-entry items that get MCQ'd
        # are common and desirable; RC items keep their rc_* subtype.
        new_subtype = item.subtype
        if new_opts and not item.subtype.startswith("rc_"):
            new_subtype = "mcq_single"
        elif not new_opts and item.subtype == "numeric_entry":
            new_subtype = "numeric_entry"

        out.append(RawItem(
            source=item.source,
            anchor=item.anchor,
            measure=item.measure,
            subtype=new_subtype,
            prompt=new_prompt,
            passage=item.passage,
            options=new_opts,
            correct_answer=new_answer,
            explanation=new_explanation,
            difficulty_level=item.difficulty_level,
            raw={"pre_reformat": {
                "prompt": item.prompt,
                "options": [{"label": l, "text": t} for l, t in item.options],
                "correct_answer": item.correct_answer,
            }, "source_row": item.raw},
        ))
    return out, calls


# ── DB upsert ──────────────────────────────────────────────────────────

def import_to_db(items: List[RawItem]) -> Tuple[int, int]:
    """Idempotent insert with (source, source_anchor) as the unique key.

    Phase 1.4 hook: every candidate is run through the two-stage dedup
    service before insert. Items that match an existing live question
    are counted as ``skipped_existing`` and the dedup service appends
    a structured-log entry.

    Returns (inserted, skipped_existing).
    """
    from models.database import db, init_db, Question, QuestionOption, NumericAnswer
    from services.dedup import get_dedup_service
    init_db()
    db.connect(reuse_if_open=True)

    dedup_svc = get_dedup_service()

    inserted = 0
    skipped = 0
    with db.atomic():
        for item in items:
            exists = (
                Question.select()
                .where((Question.source == item.source)
                       & (Question.source_anchor == item.anchor))
                .first()
            )
            if exists:
                skipped += 1
                continue

            # Phase 1.4: dedup against the live bank.
            opt_texts = [t for (_lbl, t) in item.options]
            dup_qid = dedup_svc.find_dup_for(
                prompt=item.prompt,
                stimulus_content=item.passage or "",
                options=opt_texts,
                source=item.source,
            )
            if dup_qid is not None:
                skipped += 1
                continue

            concept_tags = ["agieval_math_pipeline"]
            if item.measure == "verbal":
                concept_tags.append("lsat_derived")
            elif item.measure == "quant":
                concept_tags.append("competition_math_derived")
            if item.difficulty_level is not None:
                concept_tags.append(f"level_{item.difficulty_level}")

            # Map MATH Level 1-3 → GRE difficulty 2/3/4 roughly.
            if item.difficulty_level:
                difficulty_target = {1: 2, 2: 3, 3: 4, 4: 4, 5: 5}.get(
                    item.difficulty_level, 3)
            else:
                difficulty_target = 3

            provenance_payload = {
                "pipeline": "agieval_math",
                "source": item.source,
                "anchor": item.anchor,
                "raw": item.raw,
            }

            prompt_text = item.prompt
            if item.passage:
                # Attach passage to prompt for RC items; downstream RC UI
                # will use whichever path is cleaner.
                prompt_text = f"{item.passage}\n\n{item.prompt}"

            q = Question.create(
                measure=item.measure,
                subtype=item.subtype,
                prompt=prompt_text,
                difficulty_target=difficulty_target,
                time_target_seconds=90 if item.measure == "verbal" else 105,
                concept_tags=json.dumps(concept_tags),
                source=item.source,
                source_anchor=item.anchor,
                provenance="llm_reviewed" if item.raw.get("pre_reformat") else "imported",
                status="candidate",
                explanation=item.explanation or "",
                provenance_json=json.dumps(provenance_payload),
            )
            if item.subtype == "numeric_entry":
                try:
                    val = float(item.correct_answer) if item.correct_answer else None
                except ValueError:
                    val = None
                if val is not None:
                    NumericAnswer.create(
                        question=q, exact_value=val, mode="decimal",
                    )
            else:
                for lbl, txt in item.options:
                    QuestionOption.create(
                        question=q,
                        option_label=lbl,
                        option_text=txt,
                        is_correct=(lbl == item.correct_answer),
                    )
            inserted += 1
    return inserted, skipped


# ── Top-level orchestration ────────────────────────────────────────────

def run(source: str,
        max_items: Optional[int] = None,
        reformat: bool = False,
        dry_run: bool = False) -> Dict[str, Any]:
    """End-to-end: load -> normalize -> (optional reformat) -> (optional DB insert).

    Returns a summary dict for logging + tests.
    """
    logger.info("run: source=%s max_items=%s reformat=%s dry_run=%s",
                source, max_items, reformat, dry_run)

    if source == "agieval":
        # Convenience meta-source: do both LR + RC.
        rows_lr = load_raw_rows("agieval_lsat_lr")
        rows_rc = load_raw_rows("agieval_lsat_rc")
        items = normalize_agieval_lsat_lr(rows_lr) + normalize_agieval_lsat_rc(rows_rc)
    elif source == "agieval_lsat_lr":
        rows = load_raw_rows(source)
        items = normalize_agieval_lsat_lr(rows)
    elif source == "agieval_lsat_rc":
        rows = load_raw_rows(source)
        items = normalize_agieval_lsat_rc(rows)
    elif source == "math":
        rows = load_raw_rows("hendrycks_math")
        items = normalize_hendrycks_math(rows, max_level=3)
    else:
        raise ValueError(f"unknown --source: {source}")

    if max_items is not None:
        items = items[:max_items]

    llm_calls = 0
    if reformat and items:
        from services.llm_service import llm_service
        items, llm_calls = apply_reformat(items, llm_service)

    summary: Dict[str, Any] = {
        "source": source,
        "items_normalized": len(items),
        "llm_calls": llm_calls,
        "by_subtype": _count_by(lambda i: i.subtype, items),
        "by_source_tag": _count_by(lambda i: i.source, items),
    }

    if dry_run:
        logger.info("dry-run: skipping DB write")
        summary["inserted"] = 0
        summary["skipped"] = 0
        summary["dry_run"] = True
        return summary

    inserted, skipped = import_to_db(items)
    summary["inserted"] = inserted
    summary["skipped"] = skipped
    summary["dry_run"] = False
    return summary


def _count_by(key_fn, items: List[RawItem]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in items:
        k = key_fn(i)
        out[k] = out.get(k, 0) + 1
    return out


# ── CLI ────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AGIEval LSAT + Hendrycks MATH → GRE-reformat pipeline"
    )
    parser.add_argument(
        "--source", required=True,
        choices=["agieval", "agieval_lsat_lr", "agieval_lsat_rc", "math"],
        help="Which upstream dataset to ingest.",
    )
    parser.add_argument(
        "--max-items", type=int, default=None,
        help="Cap the number of normalized items processed (useful for smoke runs).",
    )
    parser.add_argument(
        "--reformat", action="store_true",
        help="Send each item through the LLM to rephrase into GRE register.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do everything except the final DB upsert.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable INFO-level logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    summary = run(
        source=args.source,
        max_items=args.max_items,
        reformat=args.reformat,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
