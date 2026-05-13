"""Tests for ``scripts/lib/marker_pipeline.py``.

Covers:
* Module imports even when the extractor lib is missing.
* ``extractor_available`` returns a plain bool.
* PDF extraction against a 2-page in-memory fixture writes ≥1
  non-empty markdown file.
* EPUB extraction, if ``pymupdf`` supports EPUB on this platform,
  behaves the same way. Otherwise the test is skipped.

The tests skip cleanly (rather than erroring) when the underlying
extractor lib isn't installed so CI on minimal images stays green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make repo root importable regardless of pytest invocation cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib import marker_pipeline  # noqa: E402


def test_module_imports_without_error():
    """``scripts.lib.marker_pipeline`` must be importable on any box.

    The extractor library is loaded lazily inside the public
    functions, so ``import`` itself must never fail even on a
    stripped-down CI image.
    """
    assert hasattr(marker_pipeline, "extract_pdf_to_markdown")
    assert hasattr(marker_pipeline, "extract_epub_to_markdown")
    assert hasattr(marker_pipeline, "extractor_available")


def test_extractor_available_returns_bool():
    got = marker_pipeline.extractor_available()
    assert isinstance(got, bool)


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """Build a 2-page PDF fixture with text on both pages."""
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    try:
        p1 = doc.new_page()
        p1.insert_text(
            (72, 72),
            "Page one. The quick brown fox.",
            fontsize=14,
        )
        p2 = doc.new_page()
        p2.insert_text(
            (72, 72),
            "Page two. Jumps over the lazy dog.",
            fontsize=14,
        )
        dst = tmp_path / "tiny.pdf"
        doc.save(str(dst))
    finally:
        doc.close()
    return dst


def test_extract_pdf_to_markdown_writes_non_empty_files(tmp_path, tiny_pdf):
    if not marker_pipeline.extractor_available():
        pytest.skip("pymupdf4llm not installed on this environment")

    out_dir = tmp_path / "out"
    md_files = marker_pipeline.extract_pdf_to_markdown(tiny_pdf, out_dir)

    # One markdown file per source page.
    assert len(md_files) == 2
    assert all(p.exists() for p in md_files)

    # At least one page must have non-empty content -- if both are
    # empty we've got a config problem (e.g. pymupdf4llm detecting
    # an empty page incorrectly or the fontsize being filtered).
    nonempty = [p for p in md_files if p.read_text(encoding="utf-8").strip()]
    assert nonempty, "expected at least one non-empty markdown file"

    # Verify the recognizable source text survives the extraction.
    combined = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
    assert "quick brown fox" in combined or "lazy dog" in combined

    # images/ subdir is always created even when empty -- the
    # downstream parser expects it to exist.
    assert (out_dir / "images").is_dir()


def test_extract_pdf_to_markdown_raises_on_missing_file(tmp_path):
    if not marker_pipeline.extractor_available():
        pytest.skip("pymupdf4llm not installed on this environment")

    missing = tmp_path / "does_not_exist.pdf"
    with pytest.raises(FileNotFoundError):
        marker_pipeline.extract_pdf_to_markdown(missing, tmp_path / "out")


def test_extract_epub_to_markdown_handles_minimal_epub(tmp_path):
    """If MuPDF accepts an EPUB input, the same pipeline must work.

    We construct the smallest possible EPUB (ZIP with mimetype +
    one XHTML chapter + a minimal OPF manifest). If MuPDF can't
    parse it on this platform the test skips.
    """
    if not marker_pipeline.extractor_available():
        pytest.skip("pymupdf4llm not installed on this environment")

    import zipfile

    epub_path = tmp_path / "tiny.epub"

    chapter_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE html>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>c1</title></head>"
        "<body><h1>Chapter One</h1>"
        "<p>Hello extraction world.</p>"
        "</body></html>"
    )
    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="BookId">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Tiny</dc:title>"
        '<dc:identifier id="BookId">urn:tiny</dc:identifier>'
        "<dc:language>en</dc:language>"
        "</metadata>"
        "<manifest>"
        '<item id="c1" href="c1.xhtml" '
        'media-type="application/xhtml+xml"/>'
        "</manifest>"
        '<spine><itemref idref="c1"/></spine>'
        "</package>"
    )
    container_xml = (
        '<?xml version="1.0"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as z:
        # "mimetype" must be first and uncompressed per EPUB spec.
        z.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr("META-INF/container.xml", container_xml)
        z.writestr("content.opf", content_opf)
        z.writestr("c1.xhtml", chapter_xhtml)

    try:
        md_files = marker_pipeline.extract_epub_to_markdown(
            epub_path, tmp_path / "epub_out"
        )
    except Exception as exc:  # pragma: no cover - platform-dependent
        # MuPDF EPUB support is build-flag-dependent; if the local
        # wheel was compiled without it, skip rather than fail.
        pytest.skip(f"MuPDF could not open synthetic EPUB: {exc}")

    assert len(md_files) >= 1
    assert all(p.exists() for p in md_files)
    combined = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
    assert "Chapter One" in combined or "extraction world" in combined
