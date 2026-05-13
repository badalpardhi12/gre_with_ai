"""PDF / EPUB -> structured markdown extraction layer.

Phase 4 / D3 (see ``docs/implementation_plan_2026_05_12.md``).

Background
----------
The ETS Official Guide (D1) and ETS Big Book (D2) pipelines both need
high-fidelity extraction that preserves figures, tables, and inline
math. The original plan called for ``marker-pdf`` (VikParuchuri/marker),
but its surya-ocr dependency uses Python 3.10+ union syntax and
therefore cannot import under the project's Python 3.9.6 interpreter
(see MEMORY.md "Python 3.9 Constraint").

We instead use ``pymupdf4llm`` -- the same PyMuPDF stack we already
depend on for build-time figure extraction, with a thin pure-Python
Markdown writer on top. Install size is ~30 kB (pymupdf is already a
requirement). It produces per-page markdown with image references
preserved, which is what the downstream ``scripts/extract_*.py`` parsers
need to split into ``Question`` rows.

Design notes
------------
* **Lazy imports.** The extractor library is only loaded inside the
  public functions so ``import scripts.lib.marker_pipeline`` stays
  cheap and, more importantly, does not fail on machines where
  pymupdf4llm wasn't installed. Callers that check for availability
  up front should use :func:`extractor_available`.
* **Per-page output.** We emit one ``page_NNNN.md`` per source page.
  The downstream parsers iterate those files and look for answer-key
  anchors to carve out individual items. Writing per-page (rather
  than one big concatenated file) keeps parse errors local -- a bad
  page doesn't poison the whole book.
* **Figure preservation.** Images are written under
  ``<out_dir>/images/`` and the markdown links to them with relative
  paths. These land in ``data/extracted/`` alongside the per-page
  markdown and flow into the build-time figure audit described in
  ``docs/figure_audit_2026_05_11.md`` (the audit already treats any
  ``data:image/...`` or image-bearing stimulus as high-risk regardless
  of source, so no additional wiring is needed here).
* **EPUB support.** pymupdf/MuPDF natively opens EPUB files -- the
  same ``to_markdown`` entrypoint works. We keep a separate function
  signature so callers can special-case EPUB quirks (e.g. chapter
  boundaries) later without churning the PDF path.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional


def extractor_available() -> bool:
    """Return True iff the underlying markdown-extraction library imports.

    Used by tests (to ``pytest.skip`` gracefully on fresh checkouts)
    and by scripts that want to fail fast with a helpful message.
    """
    try:
        import pymupdf4llm  # noqa: F401
        import pymupdf  # noqa: F401
    except Exception:  # pragma: no cover - defensive
        return False
    return True


def _extract_to_markdown(
    src_path: Path,
    out_dir: Path,
    *,
    kind: str,
) -> List[Path]:
    """Shared implementation for PDF and EPUB extraction.

    ``kind`` is just a tag used in the per-page filename so downstream
    tooling can tell at a glance whether a markdown chunk came from a
    PDF page or an EPUB flow.
    """
    if not extractor_available():  # pragma: no cover - import guard
        raise RuntimeError(
            "pymupdf4llm / pymupdf not installed. Run "
            "`venv/bin/pip install pymupdf4llm` and retry."
        )

    import pymupdf  # local import: heavy C extension
    import pymupdf4llm

    src_path = Path(src_path)
    out_dir = Path(out_dir)
    if not src_path.exists():
        raise FileNotFoundError(src_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir = out_dir / "images"
    images_dir.mkdir(exist_ok=True)

    doc = pymupdf.open(src_path)
    try:
        n_pages = doc.page_count
        written: List[Path] = []
        for page_idx in range(n_pages):
            # ``page_chunks=True`` returns a list of dicts, one per
            # page, with a ``text`` key holding the markdown. We call
            # it per-page to keep memory bounded on large books.
            chunks = pymupdf4llm.to_markdown(
                doc,
                pages=[page_idx],
                page_chunks=True,
                write_images=True,
                image_path=str(images_dir),
                image_format="png",
                show_progress=False,
            )
            md_text = ""
            if chunks:
                first = chunks[0]
                # pymupdf4llm returns dicts; defensively support str too.
                if isinstance(first, dict):
                    md_text = first.get("text", "") or ""
                else:
                    md_text = str(first)

            # Always write the file -- even empty pages matter because
            # they preserve the 1:1 page-index -> filename mapping that
            # the downstream answer-key parser relies on.
            fname = f"{kind}_page_{page_idx + 1:04d}.md"
            fpath = out_dir / fname
            fpath.write_text(md_text, encoding="utf-8")
            written.append(fpath)
        return written
    finally:
        doc.close()


def extract_pdf_to_markdown(
    pdf_path: Path, out_dir: Path
) -> List[Path]:
    """Extract a PDF into per-page ``.md`` files plus extracted images.

    Parameters
    ----------
    pdf_path:
        Path to a ``.pdf`` file readable by MuPDF.
    out_dir:
        Directory to write ``pdf_page_NNNN.md`` and an ``images/``
        subdir into. Created if missing.

    Returns
    -------
    list[Path]
        Absolute paths to the written markdown files, in page order.
    """
    return _extract_to_markdown(pdf_path, out_dir, kind="pdf")


def extract_epub_to_markdown(
    epub_path: Path, out_dir: Path
) -> List[Path]:
    """Extract an EPUB into per-page ``.md`` files plus extracted images.

    MuPDF treats each ``<chapter>`` flow as a page once paginated at
    the default ``page_width`` / ``page_height``. That's fine for our
    purposes -- the downstream parsers don't require chapter semantics.
    """
    return _extract_to_markdown(epub_path, out_dir, kind="epub")


__all__ = [
    "extractor_available",
    "extract_pdf_to_markdown",
    "extract_epub_to_markdown",
]
