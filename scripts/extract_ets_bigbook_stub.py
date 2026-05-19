"""End-to-end demo of the Phase 4 / D3 extraction pipeline.

This script does **not** touch the licensed ETS Big Book -- that's
D2's job. It exists to let you sanity-check the pipeline plumbing
without a real book on disk:

1. Synthesises a 2-page PDF with some text and a rectangle
   (a stand-in for a geometry figure).
2. Runs :func:`scripts.lib.marker_pipeline.extract_pdf_to_markdown`
   against it.
3. Prints the markdown filenames + first 200 chars of each.

Run it from the repo root with::

    venv/bin/python scripts/extract_ets_bigbook_stub.py

The real D2 pipeline will wrap the same extract call with a
book-specific answer-key parser, tag each resulting question with
``source='ets_big_book_2nd_ed'``, and feed the stimulus images
through the build-time figure audit flow described in
``docs/figure_audit_2026_05_11.md`` before anything goes live.

Phase 1.4 (dedup) note
----------------------
This stub does NOT perform any DB writes — it only round-trips a
synthesised PDF through marker. There is therefore no
``Question.create`` call to wrap with a dedup check. The downstream
``extract_ets_bigbook.py`` (the real D2 pipeline) is the script that
calls :func:`services.dedup.get_dedup_service` before each insert.
If you ever extend this stub to write rows, mirror the dedup hook
from ``extract_ets_bigbook.import_to_db``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Make the repo root importable when invoked as a bare script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib.marker_pipeline import (  # noqa: E402
    extract_pdf_to_markdown,
    extractor_available,
)


def _make_synthetic_pdf(dst: Path) -> None:
    """Build a 2-page PDF with text + a vector rectangle."""
    import pymupdf

    doc = pymupdf.open()
    try:
        # Page 1: prose stimulus
        p1 = doc.new_page()
        p1.insert_text(
            (72, 72),
            "Question 1. What is 2 + 2?",
            fontsize=14,
        )
        p1.insert_text(
            (72, 120),
            "(A) 3  (B) 4  (C) 5  (D) 6  (E) 7",
            fontsize=12,
        )

        # Page 2: geometry-figure stand-in
        p2 = doc.new_page()
        p2.insert_text(
            (72, 72),
            "Question 2. In the figure below, area = ?",
            fontsize=14,
        )
        # A filled rectangle -- marker/pymupdf4llm will treat this as
        # a vector graphic or emit an image reference depending on
        # detection heuristics; either is acceptable for this stub.
        rect = pymupdf.Rect(100, 150, 300, 280)
        p2.draw_rect(rect, color=(0, 0, 0), fill=(0.7, 0.7, 0.9))

        doc.save(str(dst))
    finally:
        doc.close()


def main() -> int:
    if not extractor_available():
        print(
            "ERROR: pymupdf4llm not installed. "
            "Run: venv/bin/pip install pymupdf4llm",
            file=sys.stderr,
        )
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="ets_bigbook_stub_"))
    try:
        pdf_path = workdir / "synthetic.pdf"
        out_dir = workdir / "out"
        _make_synthetic_pdf(pdf_path)
        md_files = extract_pdf_to_markdown(pdf_path, out_dir)

        print(f"Extracted {len(md_files)} page(s) from {pdf_path}:")
        for f in md_files:
            text = f.read_text(encoding="utf-8")
            preview = text.strip().replace("\n", " ")[:200]
            print(f"  {f.name}: {preview!r}")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
