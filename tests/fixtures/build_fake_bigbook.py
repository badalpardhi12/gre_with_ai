"""Build tests/fixtures/fake_bigbook.pdf — a tiny synthetic "Big Book"
used by tests/test_extract_bigbook.py.

Contains:
  - 1 test ("Test 1")
  - 1 section ("Section 1 Verbal Ability")
  - 5 items: 3 kept subtypes (TC, RC, QC) + 2 obsolete (antonym, analogy)
  - 1 answer-key block at the end of the test

Run directly with::

    venv/bin/python tests/fixtures/build_fake_bigbook.py

The test suite auto-regenerates this file via
``tests.fixtures.build_fake_bigbook.main`` whenever it's missing, so
you shouldn't need to re-run it manually.
"""
from __future__ import annotations

from pathlib import Path


FIXTURE_PATH = Path(__file__).resolve().parent / "fake_bigbook.pdf"


# One synthetic ETS Big Book test with 5 items. Written to a PDF so
# the whole marker_pipeline → parser handoff is exercised end-to-end.
#
# Content is intentionally flat text (one line per visual line) because
# pymupdf4llm produces cleaner markdown from straight text blocks than
# from heavy two-column layouts, and the fixture's purpose is parser
# coverage, not layout robustness.
PAGE_1_LINES = [
    "Test 1",
    "",
    "Section 1 Verbal Ability",
    "",
    # Item 1 — sentence completion (keeper → TC)
    "1. The scientist's claim was met with _____ skepticism by her",
    "   peers, whose own findings suggested the opposite conclusion.",
    "   (A) measured",
    "   (B) reflexive",
    "   (C) muted",
    "   (D) genuine",
    "   (E) tepid",
    "",
    # Item 2 — reading comprehension (keeper → RC)
    "Passage: The following passage discusses the author's view of",
    "recent reforms. See line 12 for the key claim.",
    "",
    "2. According to the passage, the author views the reforms with",
    "   (A) ironic detachment",
    "   (B) cautious optimism",
    "   (C) outright hostility",
    "   (D) indifference",
    "   (E) unqualified enthusiasm",
    "",
    # Item 3 — antonym (obsolete)
    "3. PLACID:",
    "   (A) turbulent",
    "   (B) serene",
    "   (C) distant",
    "   (D) frozen",
    "   (E) deep",
    "",
    # Item 4 — analogy (obsolete)
    "4. TRAIN :: LOCOMOTIVE :",
    "   (A) car : engine",
    "   (B) plane : wing",
    "   (C) ship : rudder",
    "   (D) bus : wheel",
    "   (E) bike : pedal",
    "",
    "Section 2 Quantitative Ability",
    "",
    # Item 1 in section 2 — quantitative comparison (keeper → QC)
    "1. Column A : Column B",
    "   Column A: the value of 3 + 4",
    "   Column B: the value of 2 + 5",
    "   (A) Column A is greater",
    "   (B) Column B is greater",
    "   (C) The two are equal",
    "   (D) Cannot be determined",
    "",
    "Answer Key for Test 1",
    "",
    "Section 1",
    "1. A   2. B   3. A   4. A",
    "",
    "Section 2",
    "1. C",
]


def main() -> Path:
    import pymupdf

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open()
    try:
        page = doc.new_page(width=612, height=792)  # US letter
        y = 72
        for line in PAGE_1_LINES:
            page.insert_text((72, y), line, fontsize=11)
            y += 14
            if y > 780:
                page = doc.new_page(width=612, height=792)
                y = 72
        doc.save(str(FIXTURE_PATH))
    finally:
        doc.close()

    return FIXTURE_PATH


if __name__ == "__main__":
    path = main()
    print(f"Wrote {path} ({path.stat().st_size} bytes)")
