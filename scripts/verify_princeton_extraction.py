"""Run the LLM verification pass on extracted Princeton questions.

This is the operator-facing driver for :mod:`services.extraction_verification`.
It renders a tightly cropped image of the source EPUB region for every
question (using the publisher's own asset GIFs/JPGs as the verification
target) and ships it to Sonnet 4.6 along with the structured extraction.

Phase 0 usage::

    venv/bin/python scripts/verify_princeton_extraction.py \\
        --section cgd1 --limit 5 --dry-run

The driver writes per-question verdict JSON to
``data/extracted/princeton/verification/<qid>.json`` and a summary to
``data/extracted/princeton/verification_summary.json``. It NEVER touches
the database; the caller decides whether to apply auto-corrections
(``--apply``) or just observe (``--dry-run``).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from typing import Any, Dict, List, Optional

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_REPO = "/Users/chiku/Documents/side_projects/gre_with_ai"
# Worktree path FIRST so its scripts/extract_princeton wins over the
# main checkout's scripts/ when Python resolves the import.
sys.path.insert(0, WORKTREE)
# Main repo SECOND for the local-only services._llm_gateway import.
if MAIN_REPO not in sys.path:
    sys.path.append(MAIN_REPO)

from scripts.extract_princeton import (  # noqa: E402
    EPUB_PATH as _DEFAULT_EPUB_PATH, EXTRACT_DIR,
    QUANT_DRILL_SLUGS, VERBAL_DRILL_SLUGS,
    extract_section, run_validation_gates,
)
from services.extraction_verification import (  # noqa: E402
    apply_correction, verify_question, verify_many,
)


# The worktree's data/ebooks/ doesn't ship the EPUB (gitignored, large).
# Fall back to the main checkout's copy when needed.
def _resolve_epub_path() -> str:
    if os.path.exists(_DEFAULT_EPUB_PATH):
        return _DEFAULT_EPUB_PATH
    fallback = os.path.join(
        MAIN_REPO, "data", "ebooks",
        "Princeton Review - 1,014 GRE Practice Questions, 3rd Edition-Princeton Review (2012).epub",
    )
    return fallback


EPUB_PATH = _resolve_epub_path()


VERIFICATION_DIR = os.path.join(EXTRACT_DIR, "verification")
SUMMARY_PATH = os.path.join(EXTRACT_DIR, "verification_summary.json")


# Source-region rendering ------------------------------------------------


def _read_epub_bytes(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _build_composite_png(title_text: str,
                         tiles: List[Dict[str, Any]]) -> Optional[bytes]:
    """Stack ``tiles`` (dicts with ``label``+``bytes``) into one PNG.

    Used when a question has multiple stimulus assets (e.g. a chart + an
    operator-definition glyph). Returns ``None`` if Pillow isn't available
    or no tiles render successfully.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    pil_tiles = []
    for t in tiles:
        try:
            img = Image.open(io.BytesIO(t["bytes"])).convert("RGB")
            pil_tiles.append((t.get("label", ""), img))
        except Exception:
            continue
    if not pil_tiles:
        return None
    # Title bar + tile rows.
    title_h = 32
    pad = 12
    label_h = 18
    width = max(img.width for _, img in pil_tiles)
    width = max(width, 480)
    width = min(width, 900)
    rows = []
    for label, img in pil_tiles:
        if img.width > width:
            ratio = width / float(img.width)
            new_h = max(1, int(img.height * ratio))
            img = img.resize((width, new_h))
        rows.append((label, img))
    height = title_h + sum(label_h + img.height + pad for _, img in rows) + pad
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    draw.text((6, 8), title_text, fill="black", font=font)
    y = title_h
    for label, img in rows:
        if label:
            draw.text((6, y), label, fill="black", font=font)
            y += label_h
        canvas.paste(img, ((width - img.width) // 2, y))
        y += img.height + pad
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def make_render_fn(epub_path: str = EPUB_PATH):
    """Return a render_fn(question)->bytes that pulls assets from the EPUB.

    The rendered image stitches together every figure / inline-GIF the
    question references (chart, operator-def glyph, fraction GIFs, TC
    answer-choice tables). The verifier then receives both the
    publisher's literal artwork AND our extracted JSON, so it can spot
    missing-figure and missing-inline-math defects directly.
    """
    zf = zipfile.ZipFile(epub_path)

    def render(question: Dict[str, Any]) -> Optional[bytes]:
        tiles = []
        seen = set()

        def _maybe_add(filename: str, label: str):
            if not filename or filename in seen:
                return
            seen.add(filename)
            data = _read_epub_bytes(zf, "OEBPS/images/" + filename)
            if not data:
                # Some publishers store under /images/ directly; try both.
                data = _read_epub_bytes(zf, "images/" + filename)
            if data:
                tiles.append({"label": label, "bytes": data,
                              "filename": filename})

        for fr in question.get("figure_refs") or []:
            _maybe_add(fr.get("filename"), "figure: " + (fr.get("filename") or ""))
        for ig in question.get("inline_gif_targets") or []:
            _maybe_add(ig.get("filename"),
                       "inline (" + (ig.get("context") or "") + "): "
                       + (ig.get("filename") or ""))
        ati = question.get("answer_table_image")
        if ati:
            _maybe_add(ati, "TC/SE answer-choice table: " + ati)
        for o in question.get("options") or []:
            text = o.get("text") or ""
            for m in re.finditer(r"\[img:([^\]]+)\]", text):
                _maybe_add(m.group(1),
                           "option " + (o.get("label") or "?") + ": " + m.group(1))

        if not tiles:
            return None
        title = "qst{} | {} | {}".format(
            question.get("qst_id"), question.get("subtype"),
            question.get("source_path") or "")
        return _build_composite_png(title, tiles)

    return render


# CLI --------------------------------------------------------------------


def _select_questions(args) -> List[Dict[str, Any]]:
    base_slugs = []
    if args.section:
        base_slugs = [args.section]
    else:
        base_slugs = sorted(QUANT_DRILL_SLUGS | VERBAL_DRILL_SLUGS)
    selected = []
    for slug in base_slugs:
        qs, _ = extract_section(args.epub_path, slug)
        for q in qs:
            q["source_base_slug"] = slug
        selected.extend(qs)
    if args.only_with_assets:
        selected = [q for q in selected
                    if q.get("figure_refs") or q.get("inline_gif_targets")
                    or any("[img:" in (o.get("text") or "")
                           for o in q.get("options") or [])]
    if args.qids:
        wanted = {int(x) for x in args.qids.split(",")}
        selected = [q for q in selected if q["qst_id"] in wanted]
    if args.limit:
        selected = selected[:args.limit]
    return selected


def main():
    parser = argparse.ArgumentParser(description="Verify Princeton extractions via vision LLM")
    parser.add_argument("--section", default=None,
                        help="drill base_slug (e.g. cgd, rcd) — defaults to all")
    parser.add_argument("--qids", default=None,
                        help="comma-separated QST ids to verify (overrides --section)")
    parser.add_argument("--only-with-assets", action="store_true",
                        help="skip questions with no figures/inline GIFs (cheap mode)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--budget-usd", type=float, default=25.0)
    parser.add_argument("--apply", action="store_true",
                        help="auto-apply safe corrections to in-memory dicts")
    parser.add_argument("--dry-run", action="store_true",
                        help="render images but skip the LLM call (no spend)")
    parser.add_argument("--epub-path", default=EPUB_PATH)
    parser.add_argument("--out-dir", default=VERIFICATION_DIR)
    args = parser.parse_args()

    questions = _select_questions(args)
    if not questions:
        print("no questions selected.")
        return 0
    print("verifying " + str(len(questions)) + " question(s)")

    os.makedirs(args.out_dir, exist_ok=True)
    render_fn = make_render_fn(args.epub_path)

    if args.dry_run:
        rendered = 0
        for q in questions:
            img = render_fn(q)
            if img is not None:
                rendered += 1
                # Persist a sample for visual inspection.
                fname = "qst" + str(q["qst_id"]) + ".png"
                with open(os.path.join(args.out_dir, fname), "wb") as f:
                    f.write(img)
        print("dry-run: rendered " + str(rendered) + " image(s); no LLM calls made")
        return 0

    verdicts = []
    counter = Counter()

    def progress(i, n, v):
        tag = "OK" if v.get("verified") else (
            "SKIP" if v.get("skipped") else "DEFECT")
        counter[tag] += 1
        print("  [" + str(i) + "/" + str(n) + "] " + tag
              + ("  defects=" + ",".join(v.get("defects") or [])
                 if not v.get("verified") else ""))

    verdicts = verify_many(
        questions, render_fn,
        budget_usd=args.budget_usd,
        apply=args.apply,
        on_progress=progress,
    )
    # Persist per-question verdicts.
    for q, v in zip(questions, verdicts):
        path = os.path.join(args.out_dir, "qst" + str(q["qst_id"]) + ".json")
        with open(path, "w") as f:
            json.dump({"question": {
                "qst_id": q["qst_id"],
                "subtype": q.get("subtype"),
                "prompt": q.get("prompt"),
                "options": q.get("options"),
                "correct_label": q.get("correct_label"),
                "verification_status": q.get("verification_status"),
                "correction_log": q.get("correction_log"),
            }, "verdict": v}, f, ensure_ascii=False, indent=2)

    summary = {
        "total": len(verdicts),
        "verified": sum(1 for v in verdicts if v.get("verified")),
        "defects": sum(1 for v in verdicts
                       if not v.get("verified") and not v.get("skipped")),
        "skipped": sum(1 for v in verdicts if v.get("skipped")),
        "estimated_cost_usd": round(
            sum(v.get("cost_estimate_usd", 0) for v in verdicts), 4),
        "defect_distribution": dict(Counter(
            tag for v in verdicts
            for tag in (v.get("defects") or [])
        )),
    }
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)
    print()
    print("Summary:")
    for k, v in summary.items():
        print("  " + k + ": " + str(v))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
