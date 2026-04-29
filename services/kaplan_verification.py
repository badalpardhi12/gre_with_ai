"""Kaplan-specific helpers for the LLM extraction-verification pipeline.

Bridges the deterministic Kaplan extractor to the publisher-agnostic
``services.extraction_verification`` module:

  - ``render_kaplan_question``: pulls the right image bytes out of the
    EPUB for a parsed item (figure image, option-table image, or
    multi-glyph composite) so the verifier has something to look at.
  - ``verify_kaplan_blocks``: runs the verifier across an entire phase 0
    output, collecting per-item verdicts and aggregate stats.

This module is import-safe without a network connection — the verifier
imports happen lazily inside the entry points.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── Renderer ──────────────────────────────────────────────────────────


def render_kaplan_question(
    item: Dict[str, Any],
    *,
    epub: zipfile.ZipFile,
) -> Optional[bytes]:
    """Return image bytes of the most informative artwork for this item.

    Priority order:
      1. The ``figure_image`` (the one we believe is the diagram).
      2. The first inline-glyph file (option-table image for multi-blank
         TC, or first math glyph for math-heavy items).
      3. None — caller should fall back to text-only verification.
    """
    # Try the figure first.
    fig = item.get("figure_image")
    if fig:
        b = _read_image_from_epub(epub, fig)
        if b:
            return b
    # Fall back to the first inline glyph (most likely the option-table
    # image for verbal multi-blank items).
    for g in (item.get("inline_glyph_files") or []):
        b = _read_image_from_epub(epub, g)
        if b:
            return b
    return None


def render_kaplan_figure_only(item: Dict[str, Any], *,
                              epub: zipfile.ZipFile) -> Optional[bytes]:
    """Like :func:`render_kaplan_question` but returns only the figure
    (never an option-table fallback). Used by the figure-alignment
    check, which is meaningless without an actual figure."""
    fig = item.get("figure_image")
    if not fig:
        return None
    return _read_image_from_epub(epub, fig)


def _read_image_from_epub(epub: zipfile.ZipFile, src: str) -> Optional[bytes]:
    """Look up a JPEG by filename in the EPUB's OEBPS/images/ directory."""
    if not src:
        return None
    name = src.rsplit("/", 1)[-1]
    candidates = [
        f"OEBPS/images/{name}",
        f"OEBPS/{name}",
        name,
    ]
    for c in candidates:
        try:
            return epub.read(c)
        except KeyError:
            continue
    return None


# ── Bulk verifier ────────────────────────────────────────────────────


def verify_kaplan_blocks(
    blocks_dump: Dict[str, Any],
    epub_path: str,
    *,
    sample_limit: Optional[int] = None,
    budget_usd: float = 25.0,
    cross_check_threshold: float = 0.0,
    on_progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run the LLM verification pass over a Kaplan phase0 dump.

    `blocks_dump` is the JSON object loaded from ``phase0_<chapter>.json``.
    Returns a summary dict with the verdicts per item and aggregate stats.

    `cross_check_threshold` (0.0-1.0) chooses the fraction of items
    that get the secondary-model cross-check; 0.0 disables it.
    """
    from services import extraction_verification as ev

    items: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for b in blocks_dump.get("blocks", []):
        for it in b.get("items", []):
            it["_block_id"] = (
                f"set{b.get('set_index')}:q{it.get('q_number')}"
            )
            items.append((b, it))

    if sample_limit is not None:
        items = items[:sample_limit]

    epub = zipfile.ZipFile(epub_path)

    def render(q: Dict[str, Any]) -> Optional[bytes]:
        return render_kaplan_question(q, epub=epub)

    verdicts: List[Dict[str, Any]] = []
    spent = 0.0
    figure_alignment: List[Dict[str, Any]] = []

    for i, (_b, it) in enumerate(items):
        if spent >= budget_usd:
            verdicts.append({
                "qst_id": it.get("source_ref"),
                "verified": False, "skipped": True,
                "skipped_reason": "budget_exhausted",
            })
            continue
        v = ev.verify_question(
            it, render, media_type="image/jpeg",
        )
        v["qst_id"] = it.get("source_ref")
        spent += v.get("cost_estimate_usd", 0.0)

        # If the item has an attached figure AND the stem references a
        # diagram, run the figure-alignment check too.
        if it.get("figure_image") and _stem_references_figure(it):
            fig_bytes = render_kaplan_figure_only(it, epub=epub)
            if fig_bytes is not None:
                fa = ev.check_figure_alignment(
                    it, fig_bytes, media_type="image/jpeg",
                )
                fa["qst_id"] = it.get("source_ref")
                spent += fa.get("cost_estimate_usd", 0.0)
                figure_alignment.append(fa)

        verdicts.append(v)
        if on_progress is not None:
            on_progress(i + 1, len(items), v)

    epub.close()

    # Aggregates
    n = len(verdicts)
    n_verified = sum(1 for v in verdicts if v.get("verified"))
    n_draft = sum(1 for v in verdicts if not v.get("verified")
                  and not v.get("skipped"))
    n_skipped = sum(1 for v in verdicts if v.get("skipped"))
    n_mismatch = sum(1 for fa in figure_alignment
                     if fa.get("verdict") == "mismatch")

    summary = {
        "items": n,
        "verified": n_verified,
        "draft": n_draft,
        "skipped": n_skipped,
        "figure_alignment_runs": len(figure_alignment),
        "figure_mismatches": n_mismatch,
        "cost_estimate_usd": round(spent, 4),
    }
    return {
        "verdicts": verdicts,
        "figure_alignment": figure_alignment,
        "summary": summary,
    }


_FIGURE_REF_PHRASES = (
    "the diagram", "the figure", "the graph", "the chart",
    "the table", "above", "below", "shown",
)


def _stem_references_figure(item: Dict[str, Any]) -> bool:
    """Cheap text scan: does the stem mention a figure?"""
    txt = (item.get("prompt") or "").lower()
    txt = re.sub(r"<[^>]+>", " ", txt)
    return any(p in txt for p in _FIGURE_REF_PHRASES)
