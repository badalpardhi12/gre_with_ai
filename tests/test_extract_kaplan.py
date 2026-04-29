"""Unit tests for `scripts.extract_kaplan` (Phase 0 EPUB-first parser).

These exercise the deterministic Stage A + Stage C pipelines using
synthetic XHTML fragments and a tiny stub `BeautifulSoup` chapter; we
do NOT touch the real EPUB so the tests stay fast and offline.
"""
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bs4 import BeautifulSoup

from scripts import extract_kaplan as ek
from validators import kaplan as kv


# ── Stage A — practice block detection ──────────────────────────────


def _wrap(body_html):
    """Wrap an HTML fragment in <html><body> so BeautifulSoup parses it
    cleanly with the html.parser backend (no doctype required)."""
    return BeautifulSoup(
        f"<html><body>{body_html}</body></html>", "html.parser"
    )


PRACTICE_TC_HTML = """
<h1 class="chapter-number">CHAPTER 5</h1>
<h1 class="chapter-title">TEXT COMPLETION</h1>
<h1 class="h1">Introduction to Text Completion</h1>
<p class="tx1">intro narrative</p>
<h1 class="h1">Text Completion Practice Set</h1>
<p class="tx1">Try these practice items.</p>
<ol class="ol0">
<li class="li-1">The giant squid's body is _________ .
<p class="hang-1"><img alt="image" class="inline" src="images/a.jpg"/> meaningful</p>
<p class="hang-1"><img alt="image" class="inline" src="images/b.jpg"/> elusive</p>
<p class="hang-1"><img alt="image" class="inline" src="images/c.jpg"/> popular</p>
<p class="hang-1"><img alt="image" class="inline" src="images/d.jpg"/> expensive</p>
<p class="hang-1"><img alt="image" class="inline" src="images/e.jpg"/> profitable</p>
</li>
<li class="li-1">A two-blank TC item: (i) _________ science. (ii) _________ contributions.
<p class="txc"><img alt="image" src="images/p65a.jpg"/></p>
</li>
</ol>
<h1 class="h1">Text Completion Practice Set Answer Key</h1>
<ol class="ol0 bold">
<li>B</li>
<li>C, D</li>
</ol>
<h1 class="h1">Text Completion Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B</b></p>
<p class="tx1">The squid is hard to find. Choice (B) elusive matches.</p>
<p class="tx1-1"><b>2. C, D</b></p>
<p class="tx1">First blank: outweigh. Second blank: substantial.</p>
"""


def test_split_into_blocks_finds_one_triplet():
    soup = _wrap(PRACTICE_TC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter05", "Text Completion")
    assert len(triplets) == 1
    practice_h1, ak_h1, expl_h1 = triplets[0]
    assert "Practice Set" in practice_h1.get_text()
    assert "Answer Key" in ak_h1.get_text()
    assert "Explanations" in expl_h1.get_text()


def test_practice_set_yields_two_items_with_options():
    soup = _wrap(PRACTICE_TC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter05", "Text Completion")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter05",
        chapter_title="Text Completion", set_index=1,
    )
    assert block.measure == "verbal"
    assert len(block.items) == 2
    item1, item2 = block.items
    assert item1.q_number == 1
    assert len(item1.options) == 5
    assert {o.label for o in item1.options} == {"A", "B", "C", "D", "E"}
    assert item1.correct_label == "B"
    # Item 2 has no inline-letter options (the option table is one
    # JPEG); it should pick up the inline_glyph_files entry.
    assert item2.q_number == 2
    assert "p65a.jpg" in item2.inline_glyph_files
    assert item2.correct_label == "C, D"


def test_post_process_marks_correct_options():
    soup = _wrap(PRACTICE_TC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter05", "Text Completion")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter05",
        chapter_title="Text Completion", set_index=1,
    )
    ek.post_process_block(block)
    item1 = block.items[0]
    correct = [o.label for o in item1.options if o.is_correct]
    assert correct == ["B"]


def test_explanation_is_captured_as_html_with_tags():
    soup = _wrap(PRACTICE_TC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter05", "Text Completion")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter05",
        chapter_title="Text Completion", set_index=1,
    )
    ek.post_process_block(block)
    expl = block.items[0].explanation
    assert "elusive" in expl
    assert "<b>" in expl  # tx1-1 header preserved
    assert "<p" in expl


# ── Stage A — RC clustering ─────────────────────────────────────────

RC_HTML = """
<h1 class="h1">Reading Comprehension Practice Set</h1>
<h3>Questions 1-3 are based on the passage below.</h3>
<p class="tx1">The passage talks about marine biology research.</p>
<p class="tx1">Specifically deep-sea octopus colonies and their habitats.</p>
<ol class="ol0">
<li class="li-1">Which of the following is the main idea?
<p class="hang-1"><img alt="image" class="inline" src="images/a.jpg"/> octopus migration</p>
<p class="hang-1"><img alt="image" class="inline" src="images/b.jpg"/> deep-sea life</p>
<p class="hang-1"><img alt="image" class="inline" src="images/c.jpg"/> shallow tidepools</p>
<p class="hang-1"><img alt="image" class="inline" src="images/d.jpg"/> coral reefs</p>
<p class="hang-1"><img alt="image" class="inline" src="images/e.jpg"/> fisheries</p>
</li>
<li class="li-1">Which best describes the passage's tone?
<p class="hang-1"><img alt="image" class="inline" src="images/a.jpg"/> celebratory</p>
<p class="hang-1"><img alt="image" class="inline" src="images/b.jpg"/> investigative</p>
<p class="hang-1"><img alt="image" class="inline" src="images/c.jpg"/> dismissive</p>
<p class="hang-1"><img alt="image" class="inline" src="images/d.jpg"/> alarmed</p>
<p class="hang-1"><img alt="image" class="inline" src="images/e.jpg"/> ironic</p>
</li>
<li class="li-1">According to the passage, deep-sea life is...
<p class="hang-1"><img alt="image" class="inline" src="images/a.jpg"/> rare</p>
<p class="hang-1"><img alt="image" class="inline" src="images/b.jpg"/> understudied</p>
<p class="hang-1"><img alt="image" class="inline" src="images/c.jpg"/> well-mapped</p>
<p class="hang-1"><img alt="image" class="inline" src="images/d.jpg"/> commercial</p>
<p class="hang-1"><img alt="image" class="inline" src="images/e.jpg"/> hostile</p>
</li>
</ol>
<h1 class="h1">Reading Comprehension Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>B</li><li>B</li><li>B</li></ol>
<h1 class="h1">Reading Comprehension Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B</b></p><p class="tx1">deep-sea life is the focus.</p>
<p class="tx1-1"><b>2. B</b></p><p class="tx1">an investigative tone.</p>
<p class="tx1-1"><b>3. B</b></p><p class="tx1">deep-sea life is understudied.</p>
"""


def test_rc_cluster_detection_assigns_shared_group():
    soup = _wrap(RC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter07", "RC")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter07",
        chapter_title="RC", set_index=1,
    )
    assert len(block.rc_groups) == 1
    grp = block.rc_groups[0]
    assert (grp.q_start, grp.q_end) == (1, 3)
    for it in block.items:
        assert it.rc_group_key == (1, 3)
        assert it.subtype == "rc_single"


# ── Stage A — Quant (numeric_entry + figures) ────────────────────────

QUANT_HTML = """
<h1 class="h1">Ratios Practice Set</h1>
<h2>Basic</h2>
<ol class="ol0">
<li class="li-1">17 is what percent of 85?</li>
<li class="li-1">Compute the value of <img alt="image" class="inline" src="images/226c.jpg"/>.
</li>
</ol>
<h2>Intermediate</h2>
<ol class="ol0" start="3">
<li class="li-1">If the average of 6, 3, and x is 5, what is x?</li>
</ol>
<h1 class="h1">Ratios Practice Set Answer Key</h1>
<ol class="ol0 bold">
<li>20%</li>
<li>1/3</li>
<li>6</li>
</ol>
<h1 class="h1">Ratios Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1.</b> 20%</p>
<p class="tx1">17/85 = 20 percent. Convert the fraction to a percent.</p>
<p class="tx1-1"><b>2.</b> 1/3</p>
<p class="tx1">Sum the fractions over a common denominator.</p>
<p class="tx1-1"><b>3.</b> 6</p>
<p class="tx1">Sum is 15; subtract 9 to recover x = 6.</p>
"""


def test_quant_practice_set_numeric_subtypes_and_difficulty_band():
    soup = _wrap(QUANT_HTML)
    triplets = ek.split_into_blocks(soup, "chapter11", "Ratios")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter11",
        chapter_title="Ratios", set_index=1,
    )
    ek.post_process_block(block)
    assert block.measure == "quant"
    assert len(block.items) == 3
    assert all(it.subtype == "numeric_entry" for it in block.items)
    assert block.items[0].difficulty_band == "Basic"
    assert block.items[2].difficulty_band == "Intermediate"
    assert block.items[0].correct_label == "20%"
    assert block.items[1].correct_label == "1/3"


# ── Stage C — Cleaners ──────────────────────────────────────────────


def test_clean_money_dollars_collapses_artefact():
    s = "Total was $$50$ before tax."
    assert ek.clean_money_dollars(s) == "Total was $50 before tax."


def test_normalise_latex_swaps_display_to_inline():
    assert ek.normalise_latex("\\[x^2\\]") == "\\(x^2\\)"
    assert ek.normalise_latex("\\$50") == "$50"


def test_parse_numeric_value_handles_common_forms():
    assert ek.parse_numeric_value("7") == 7.0
    assert ek.parse_numeric_value("4,800") == 4800.0
    assert ek.parse_numeric_value("0.25") == 0.25
    assert ek.parse_numeric_value("1/3") == pytest.approx(1 / 3)
    assert ek.parse_numeric_value("\\sqrt{4}") == 2.0
    assert ek.parse_numeric_value("\\frac{3}{4}") == 0.75
    assert ek.parse_numeric_value("7.5 gallons") == 7.5
    assert ek.parse_numeric_value(None) is None
    assert ek.parse_numeric_value("not a number") is None


def test_dedupe_options_drops_case_insensitive_duplicates():
    opts = [
        ek.RawOption("A", "Quantity A is greater."),
        ek.RawOption("B", "quantity a is greater."),
        ek.RawOption("C", "Different option."),
    ]
    out = ek.dedupe_options(opts)
    assert len(out) == 2


def test_truncate_explanation_caps_long_text():
    long = "<p>" + "x" * 8000 + "</p>"
    out = ek.truncate_explanation(long)
    assert len(out.encode("utf-8")) <= ek.MAX_EXPLANATION_BYTES + 100
    assert "<!-- truncated -->" in out


# ── Stage B — Glyph substitution ────────────────────────────────────


def test_apply_glyph_substitutions_swaps_inline_imgs_for_latex():
    html = (
        '<p>The value of <img alt="image" class="inline" '
        'src="images/p1c.jpg"/> equals 3.</p>'
    )
    cache = {"p1c.jpg": {"id": "p1c.jpg", "kind": "latex",
                          "latex": "\\frac{1}{3}"}}
    out = ek.apply_glyph_substitutions(html, cache)
    assert "p1c.jpg" not in out
    assert "\\(\\frac{1}{3}\\)" in out


def test_apply_glyph_substitutions_skips_option_letter_glyphs():
    html = '<p><img class="inline" src="images/a.jpg"/> answer text</p>'
    cache = {"a.jpg": {"id": "a.jpg", "kind": "latex", "latex": "GARBAGE"}}
    out = ek.apply_glyph_substitutions(html, cache)
    assert "a.jpg" in out  # untouched
    assert "GARBAGE" not in out


# ── Stage A — Figure-vs-glyph structural detection ──────────────────


def test_is_paragraph_lone_image_recognises_figure():
    """A txc paragraph whose only meaningful content is an image is a
    figure (or option-table image). Captions like 'Note:' / 'Figure not
    drawn to scale' don't disqualify it."""
    soup = BeautifulSoup(
        '<div><p class="txc"><img alt="diagram" class="inline" '
        'src="images/325b.jpg"/></p>'
        '<p class="txc"><u>Note</u>: Figure not drawn to scale.</p></div>',
        "html.parser",
    )
    figure_p = soup.find_all("p", class_="txc")[0]
    caption_p = soup.find_all("p", class_="txc")[1]
    assert ek._is_paragraph_lone_image(figure_p) is True
    # Caption-only paragraph should NOT be classified as a lone image.
    assert ek._is_paragraph_lone_image(caption_p) is False


def test_is_paragraph_lone_image_rejects_inline_glyph_in_sentence():
    """An image inside a sentence is a math glyph, not a figure."""
    soup = BeautifulSoup(
        '<p>The value of <img class="inline" src="images/p1c.jpg"/> equals 3.</p>',
        "html.parser",
    )
    p = soup.find("p")
    assert ek._is_paragraph_lone_image(p) is False


def test_is_option_letter_glyph_recognises_publisher_bullets():
    assert ek._is_option_letter_glyph("a.jpg")
    assert ek._is_option_letter_glyph("b.jpg")
    assert ek._is_option_letter_glyph("e.jpg")
    assert ek._is_option_letter_glyph("s-a.jpg")
    assert ek._is_option_letter_glyph("ga.jpg")
    assert ek._is_option_letter_glyph("abcd.jpg")
    assert ek._is_option_letter_glyph("37a.jpg")


def test_is_option_letter_glyph_rejects_page_prefixed_diagrams():
    assert not ek._is_option_letter_glyph("325b.jpg")
    assert not ek._is_option_letter_glyph("p65a.jpg")
    assert not ek._is_option_letter_glyph("352b.jpg")


# Synthetic ch15-style geometry item with a lone-image figure.
GEOMETRY_HTML = """
<h1 class="h1">Geometry Practice Set</h1>
<h2>Basic</h2>
<ol class="ol0">
<li class="li-1">In the diagram above, what is the value of <i>a</i>?
<p class="tx1-1"><img alt="image" class="inline" src="images/325b.jpg"/></p>
</li>
</ol>
<h1 class="h1">Geometry Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>52</li></ol>
<h1 class="h1">Geometry Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. 52</b></p>
<p class="tx1">Vertical angles are congruent.</p>
"""


def test_geometry_item_promotes_figure_image_root_cause_b_and_c():
    """Defects (b) + (c): figures that live in `<p class="tx1-1"><img
    class="inline">` should populate `figure_image`, not be treated as
    inline math glyphs. Mirrors ch15 q4 / ch17 q2 in the real book."""
    soup = _wrap(GEOMETRY_HTML)
    triplets = ek.split_into_blocks(soup, "chapter15", "Geometry")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter15",
        chapter_title="Geometry", set_index=1,
    )
    ek.post_process_block(block)
    item = block.items[0]
    assert item.figure_image == "325b.jpg"
    assert item.has_figure is True
    # The figure should NOT also appear as an inline glyph (no double-render).
    assert "325b.jpg" not in item.inline_glyph_files
    # And the prompt HTML should no longer contain the inline <img>.
    assert "325b.jpg" not in item.prompt


# ── Stage A — ch16 (QC) table-walker ────────────────────────────────


CH16_QC_HTML = """
<h1 class="h1">Quantitative Comparison Practice Set</h1>
<table class="table">
<tr><td>1.</td><td>Quantity A</td><td>Quantity B</td></tr>
<tr><td></td><td>x^2 + 2x - 2</td><td>x^2 + 2x - 1</td></tr>
</table>
<p class="tx1-1">x = 2y; y is a positive integer.</p>
<table class="table">
<tr><td>2.</td><td>Quantity A</td><td>Quantity B</td></tr>
<tr><td></td><td>4y</td><td>x</td></tr>
</table>
<h1 class="h1">Quantitative Comparison Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>B</li><li>C</li></ol>
<h1 class="h1">Quantitative Comparison Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B</b></p><p class="tx1">When x is positive, B is greater.</p>
<p class="tx1-1"><b>2. C</b></p><p class="tx1">Substitute and simplify.</p>
"""


def test_ch16_qc_table_walker_synthesises_qc_items():
    """Defect #5: ch16 lays out QC items in <table class="table"> blocks
    rather than <ol>. The synthetic walker should produce 2 qc items
    with the canonical 4-option set."""
    soup = _wrap(CH16_QC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter16", "QC")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter16",
        chapter_title="QC", set_index=1,
    )
    ek.post_process_block(block)
    assert len(block.items) == 2
    assert all(it.subtype == "qc" for it in block.items)
    assert all(len(it.options) == 4 for it in block.items)
    # Quantity A and Quantity B labels must appear in the prompt for V11.
    for it in block.items:
        assert "Quantity A" in it.prompt
        assert "Quantity B" in it.prompt
    # Answer key was applied.
    assert block.items[0].correct_label == "B"
    assert block.items[1].correct_label == "C"


# ── Stage A — Subtype routing for free-text quant + select-passage ───

QUANT_FREE_TEXT_HTML = """
<h1 class="h1">Ratios Practice Set</h1>
<ol class="ol0">
<li class="li-1">If <i>A</i>:<i>B</i> is 3:7, what is <i>A</i>:<i>D</i>?</li>
<li class="li-1">Hannah pays $50 for tickets. What was the total?</li>
<li class="li-1">If −1 &lt; x &lt; 1, which has the greater value, |x^4| or |x^5|?</li>
<li class="li-1">Compute the value of x.</li>
</ol>
<h1 class="h1">Ratios Practice Set Answer Key</h1>
<ol class="ol0 bold">
<li>18:11</li>
<li>$50</li>
<li>|x^4|</li>
<li>6</li>
</ol>
<h1 class="h1">Ratios Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. 18:11</b></p><p class="tx1">Sample reason long enough.</p>
<p class="tx1-1"><b>2. $50</b></p><p class="tx1">Money explanation goes here long.</p>
<p class="tx1-1"><b>3. |x^4|</b></p><p class="tx1">Positive base raised.</p>
<p class="tx1-1"><b>4. 6</b></p><p class="tx1">Solve linear equation.</p>
"""


def test_quant_free_text_answers_route_to_mcq_short_answer():
    """Defect #3: ratios / dollars / comparisons / units in the answer
    key should route to mcq_short_answer so V8 doesn't fail."""
    soup = _wrap(QUANT_FREE_TEXT_HTML)
    triplets = ek.split_into_blocks(soup, "chapter11", "Ratios")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter11",
        chapter_title="Ratios", set_index=1,
    )
    ek.post_process_block(block)
    subtypes = [it.subtype for it in block.items]
    # 18:11 → mcq_short_answer (ratio)
    assert subtypes[0] == "mcq_short_answer"
    # $50 → mcq_short_answer (money)
    assert subtypes[1] == "mcq_short_answer"
    # comparison ("which has greater value") → mcq_short_answer
    assert subtypes[2] == "mcq_short_answer"
    # 6 → numeric_entry (clean integer)
    assert subtypes[3] == "numeric_entry"


RC_SELECT_PASSAGE_HTML = """
<h1 class="h1">Reading Comprehension Practice Set</h1>
<h3>Question 1 is based on the passage below.</h3>
<p class="tx1">Sample passage about economics.</p>
<p class="tx1">More passage text continuing.</p>
<ol class="ol0">
<li class="li-1">Select the sentence in the passage that illustrates an abstract concept.</li>
</ol>
<h1 class="h1">Reading Comprehension Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>For example, a manufacturer might be willing to sell 7,000 sprockets if each one sells for $0.45 but would be willing to sell substantially more sprockets for a higher price.</li></ol>
<h1 class="h1">Reading Comprehension Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. For example, a manufacturer might be willing to sell 7,000 sprockets if each one sells for $0.45 but would be willing to sell substantially more sprockets for a higher price.</b></p>
<p class="tx1">This sentence illustrates the abstract concept of price elasticity.</p>
"""


def test_select_the_sentence_routes_to_rc_select_passage():
    """Defect #7: ch07 select-the-sentence items have long sentence
    answer-keys and no extracted options. Route to rc_select_passage
    so V8 doesn't fire."""
    soup = _wrap(RC_SELECT_PASSAGE_HTML)
    triplets = ek.split_into_blocks(soup, "chapter07", "RC")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter07",
        chapter_title="RC", set_index=1,
    )
    ek.post_process_block(block)
    assert block.items[0].subtype == "rc_select_passage"


MCQ_MULTI_HTML = """
<h1 class="h1">Problem Solving Practice Set</h1>
<ol class="ol0">
<li class="li-1">Which of the following numbers has more than two distinct prime factors? Indicate all such numbers.
<p class="hang-1"><img alt="image" class="inline" src="images/a.jpg"/> 20</p>
<p class="hang-1"><img alt="image" class="inline" src="images/b.jpg"/> 30</p>
<p class="hang-1"><img alt="image" class="inline" src="images/c.jpg"/> 100</p>
<p class="hang-1"><img alt="image" class="inline" src="images/d.jpg"/> 200</p>
<p class="hang-1"><img alt="image" class="inline" src="images/e.jpg"/> 210</p>
</li>
</ol>
<h1 class="h1">Problem Solving Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>B, E</li></ol>
<h1 class="h1">Problem Solving Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B, E</b></p><p class="tx1">30 = 2 x 3 x 5; 210 = 2 x 3 x 5 x 7.</p>
"""


def test_indicate_all_routes_to_mcq_multi():
    """Stems that say "Indicate all" + comma-separated answer key
    should route to mcq_multi, not mcq_single (V4 would fail)."""
    soup = _wrap(MCQ_MULTI_HTML)
    triplets = ek.split_into_blocks(soup, "chapter17", "PS")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter17",
        chapter_title="PS", set_index=1,
    )
    ek.post_process_block(block)
    assert block.items[0].subtype == "mcq_multi"
    # Both correct options should be flagged.
    correct = [o.label for o in block.items[0].options if o.is_correct]
    assert sorted(correct) == ["B", "E"]


# ── Stage A — hang-1k option-row (ch16 / ch18 layout) ───────────────


HANG_1K_HTML = """
<h1 class="h1">Data Interp Practice Set</h1>
<ol class="ol0">
<li class="li-1">Compute the percent.
<p class="hang-1k">8%<img alt="image" class="inline" src="images/a.jpg"/></p>
<p class="hang-1k">10%<img alt="image" class="inline" src="images/b.jpg"/></p>
<p class="hang-1k">11%<img alt="image" class="inline" src="images/c.jpg"/></p>
<p class="hang-1k">33%<img alt="image" class="inline" src="images/d.jpg"/></p>
<p class="hang-1k">90%<img alt="image" class="inline" src="images/e.jpg"/></p>
</li>
</ol>
<h1 class="h1">Data Interp Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>B</li></ol>
<h1 class="h1">Data Interp Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B</b></p>
<p class="tx1">Subtract and divide.</p>
"""


def test_hang_1k_extracts_options():
    """ch16 / ch18 use hang-1k for options. The label glyph follows the
    text in this layout — we still need 5 labelled options."""
    soup = _wrap(HANG_1K_HTML)
    triplets = ek.split_into_blocks(soup, "chapter18", "DI")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter18",
        chapter_title="DI", set_index=1,
    )
    ek.post_process_block(block)
    assert len(block.items[0].options) == 5
    assert {o.label for o in block.items[0].options} == {
        "A", "B", "C", "D", "E",
    }


# ── Stage A — difficulty band default ───────────────────────────────


def test_default_difficulty_band_is_medium():
    """Defect #8: ch07 / ch08 don't ship band subdividers; coerce
    None → medium so persistence has a stable signal."""
    soup = _wrap(RC_HTML)
    triplets = ek.split_into_blocks(soup, "chapter07", "RC")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter07",
        chapter_title="RC", set_index=1,
    )
    ek.post_process_block(block)
    for it in block.items:
        assert it.difficulty_band == "medium"


# ── Stage A — DI cluster figure attachment ──────────────────────────


DI_CLUSTER_HTML = """
<h1 class="h1">Data Interpretation Practice Set</h1>
<p class="tx1-1">Questions 1-3 are based on the following graphs.</p>
<p class="txc"><img alt="image" class="inline" src="images/368a.jpg"/></p>
<ol class="ol0">
<li class="li-1">What is the average?
<p class="hang-1k">10%<img alt="image" class="inline" src="images/a.jpg"/></p>
<p class="hang-1k">20%<img alt="image" class="inline" src="images/b.jpg"/></p>
<p class="hang-1k">30%<img alt="image" class="inline" src="images/c.jpg"/></p>
<p class="hang-1k">40%<img alt="image" class="inline" src="images/d.jpg"/></p>
<p class="hang-1k">50%<img alt="image" class="inline" src="images/e.jpg"/></p>
</li>
<li class="li-1">What is the median?
<p class="hang-1k">12%<img alt="image" class="inline" src="images/a.jpg"/></p>
<p class="hang-1k">25%<img alt="image" class="inline" src="images/b.jpg"/></p>
<p class="hang-1k">35%<img alt="image" class="inline" src="images/c.jpg"/></p>
<p class="hang-1k">45%<img alt="image" class="inline" src="images/d.jpg"/></p>
<p class="hang-1k">55%<img alt="image" class="inline" src="images/e.jpg"/></p>
</li>
<li class="li-1">What is the mode?
<p class="hang-1k">11%<img alt="image" class="inline" src="images/a.jpg"/></p>
<p class="hang-1k">22%<img alt="image" class="inline" src="images/b.jpg"/></p>
<p class="hang-1k">33%<img alt="image" class="inline" src="images/c.jpg"/></p>
<p class="hang-1k">44%<img alt="image" class="inline" src="images/d.jpg"/></p>
<p class="hang-1k">55%<img alt="image" class="inline" src="images/e.jpg"/></p>
</li>
</ol>
<h1 class="h1">Data Interpretation Practice Set Answer Key</h1>
<ol class="ol0 bold"><li>B</li><li>C</li><li>A</li></ol>
<h1 class="h1">Data Interpretation Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. B</b></p><p class="tx1">From the chart, the average is 20.</p>
<p class="tx1-1"><b>2. C</b></p><p class="tx1">The median value is 35.</p>
<p class="tx1-1"><b>3. A</b></p><p class="tx1">The most frequent is 11.</p>
"""


def test_di_cluster_figure_attached_to_rc_group():
    """Defect (d): DI clusters need their chart attached as a Stimulus
    asset. Verify both the cluster detection and the figure_images list."""
    soup = _wrap(DI_CLUSTER_HTML)
    triplets = ek.split_into_blocks(soup, "chapter18", "DI")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter18",
        chapter_title="DI", set_index=1,
    )
    ek.post_process_block(block)
    assert len(block.rc_groups) == 1
    grp = block.rc_groups[0]
    assert (grp.q_start, grp.q_end) == (1, 3)
    assert grp.kind in ("graph", "graphs")  # singular OR plural
    assert "368a.jpg" in grp.figure_images
    # All 3 items should share the cluster.
    for it in block.items:
        assert it.rc_group_key == (1, 3)
        assert it.subtype == "mcq_single"
    # Per-item figure_image should NOT duplicate the cluster chart
    # (the cluster figure is captured at the group level, not the item).
    for it in block.items:
        assert it.figure_image is None or it.figure_image == "368a.jpg"


REFER_TO_HTML = """
<h1 class="h1">DI Practice Set</h1>
<p class="tx1-1">Questions 4-6 refer to the following stimulus.</p>
<p class="txc"><img alt="image" class="inline" src="images/370b.jpg"/></p>
<ol class="ol0" start="4">
<li class="li-1">What's the trend?
<p class="hang-1k">Up<img alt="image" class="inline" src="images/a.jpg"/></p>
<p class="hang-1k">Down<img alt="image" class="inline" src="images/b.jpg"/></p>
<p class="hang-1k">Flat<img alt="image" class="inline" src="images/c.jpg"/></p>
<p class="hang-1k">Mixed<img alt="image" class="inline" src="images/d.jpg"/></p>
<p class="hang-1k">Cyclical<img alt="image" class="inline" src="images/e.jpg"/></p>
</li>
</ol>
<h1 class="h1">DI Practice Set Answer Key</h1>
<ol class="ol0 bold" start="4"><li>A</li></ol>
<h1 class="h1">DI Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>4. A</b></p><p class="tx1">The trend is up.</p>
"""


def test_rc_cluster_header_recognises_refer_to_stimulus():
    """Broaden the RC cluster header regex so 'Questions N-M refer to
    the following stimulus.' is recognised alongside 'are based on'."""
    soup = _wrap(REFER_TO_HTML)
    triplets = ek.split_into_blocks(soup, "chapter18", "DI")
    block = ek.parse_practice_set(
        *triplets[0], chapter_id="chapter18",
        chapter_title="DI", set_index=1,
    )
    assert len(block.rc_groups) == 1
    grp = block.rc_groups[0]
    assert (grp.q_start, grp.q_end) == (4, 6)
    assert "370b.jpg" in grp.figure_images


# ── Stage B / Stage C — explanation glyph collection + bare opener ───


def test_parse_explanations_handles_bare_number_glyph_label_opener():
    """ch15 q3-style explanations: <b>3.</b><img class="inline" src="..."/>
    used to bleed into the previous question because the opener regex
    required a non-empty label after '3.'. Verify the bare-number opener
    is recognised."""
    from bs4 import BeautifulSoup as BS
    html = """
<h1>Practice Set Answers and Explanations</h1>
<p class="tx1-1"><b>1. C, D</b></p>
<p class="tx1">First explanation body.</p>
<p class="tx1-1"><b>2.</b> 60</p>
<p class="tx1">Second explanation body.</p>
<p class="tx1-1"><b>3.</b> <img class="inline" src="images/328c.jpg"/></p>
<p class="tx1">Third explanation body.</p>
"""
    soup = BS(f"<html><body>{html}</body></html>", "html.parser")
    h1 = soup.find("h1")
    parsed = ek.parse_explanations(h1, stop_h1=None)
    assert set(parsed.keys()) == {1, 2, 3}
    assert "First" in parsed[1]["html"]
    assert "Second" in parsed[2]["html"]
    assert "Third" in parsed[3]["html"]
    # The third opener carries the glyph filename as the label.
    assert "328c.jpg" in parsed[3]["label"]


def test_explanation_glyph_collection_extends_to_explanation_html():
    """Defect #2 / (a): inline <img class="inline"> images in the
    explanation HTML should be transcribed by Stage B alongside the
    prompt/option glyphs. We verify by feeding a fake glyph cache
    and confirming the explanation gets the LaTeX substitution."""
    item_html_expl = (
        '<p class="tx1-1"><b>1. 4</b></p>'
        '<p class="tx1">Dividing by '
        '<img class="inline" src="images/p1c.jpg"/> equals…</p>'
    )
    cache = {"p1c.jpg": {"id": "p1c.jpg", "kind": "latex",
                          "latex": "\\frac{1}{3}"}}
    out = ek.apply_glyph_substitutions(item_html_expl, cache)
    assert "p1c.jpg" not in out
    assert "\\(\\frac{1}{3}\\)" in out


# ── Stage C — Unicode minus normalisation ────────────────────────────


def test_parse_numeric_value_handles_unicode_minus():
    """Kaplan ships the publisher's typographic minus (U+2212) in many
    answers; parse_numeric_value should normalise it to ASCII -."""
    assert ek.parse_numeric_value("\u22126") == -6.0
    assert ek.parse_numeric_value("\u22120.5") == -0.5


# ── Stage A — quant short_answer routing for symbolic answers ───────


def test_quant_glyph_only_answer_routes_to_mcq_short_answer():
    """When the answer key text is `@@GLYPH:p155b.jpg@@` (a JPEG that
    has yet to be transcribed), route to mcq_short_answer because
    glyph answers are almost always symbolic / LaTeX."""
    soup = BeautifulSoup(
        "<li>The area of a circle is 36. What is the diameter?</li>",
        "html.parser",
    )
    li = soup.find("li")
    sub = ek._detect_subtype_from_li(
        li, "quant", "chapter15", [], "<p>...</p>",
        answer_key_text="@@GLYPH:328c.jpg@@",
    )
    assert sub == "mcq_short_answer"


def test_quant_coordinate_pair_routes_to_mcq_short_answer():
    """Defect #3 root-cause regression: a coordinate-pair answer like
    `(-5,-8)` (with Unicode minus) should not route to numeric_entry."""
    soup = BeautifulSoup(
        "<li>What are the coordinates of the midpoint?</li>",
        "html.parser",
    )
    li = soup.find("li")
    sub = ek._detect_subtype_from_li(
        li, "quant", "chapter15", [], "<p>...</p>",
        answer_key_text="(\u22125,\u22128)",
    )
    assert sub == "mcq_short_answer"


# ── Defect (b) — sup/sub preservation in cell text ─────────────────


def test_normalise_text_with_supsub_preserves_exponents():
    """Regression for the user's Q13 complaint ('still not seeing
    superscript and subscript'). The cell text 'x<sup>2</sup>+2x-2'
    used to render as 'x 2 + 2x − 2'; new helper must keep '^{2}'."""
    soup = BeautifulSoup(
        "<td><i>x</i><sup>2</sup> + 2<i>x</i> &#8722; 2</td>",
        "html.parser",
    )
    td = soup.find("td")
    out = ek._normalise_text_with_supsub(td)
    assert "x^{2}" in out
    assert "x 2" not in out


def test_normalise_text_with_supsub_handles_subscripts():
    soup = BeautifulSoup(
        "<p>angle <i>a</i><sub>1</sub> equals 90</p>", "html.parser")
    out = ek._normalise_text_with_supsub(soup.find("p"))
    assert "a_{1}" in out


def test_normalise_text_with_supsub_emits_image_placeholders():
    """QC quantity cells often hold a single <img> as their value;
    the helper must surface that as '[img:foo.jpg]' so the QC
    synthesiser can re-emit a real <img> tag for Stage B vision."""
    soup = BeautifulSoup(
        '<td><img class="inline" src="images/337f.jpg"/></td>',
        "html.parser",
    )
    out = ek._normalise_text_with_supsub(soup.find("td"))
    assert out == "[img:337f.jpg]"


# ── Defect (c) — QC centred-info binds to the NEXT numbered Q ───────


_QC_TWO_TABLES_HTML = """
<html><body>
<table class="table">
<tbody>
<tr><td>1.</td><td class="tdc"><u>Quantity A</u></td><td class="tdc"><u>Quantity B</u></td><td/></tr>
<tr><td/><td class="tdc"><i>x</i><sup>2</sup> + 2<i>x</i> &#8722; 2</td>
       <td class="tdc"><i>x</i><sup>2</sup> + 2<i>x</i> &#8722; 1</td>
       <td><img class="inline" src="images/337e.jpg"/></td></tr>
<tr><td colspan="4"><br/></td></tr>
<tr><td/><td class="tdc" colspan="2"><i>x</i> = 2<i>y</i>; <i>y</i> is a positive integer.</td><td/></tr>
<tr><td colspan="4"><br/></td></tr>
<tr><td>2.</td><td class="tdc"><u>Quantity A</u></td><td class="tdc"><u>Quantity B</u></td><td/></tr>
<tr><td/><td class="tdc">4<sup><i>y</i></sup></td>
       <td class="tdc"><img class="inline" src="images/337f.jpg"/></td>
       <td><img class="inline" src="images/337e.jpg"/></td></tr>
<tr><td colspan="4"><br/></td></tr>
<tr><td/><td class="tdc" colspan="2"><i>q</i>, <i>r</i>, and <i>s</i> are positive numbers; <i>qrs</i> &gt; 12.</td><td/></tr>
<tr><td colspan="4"><br/></td></tr>
<tr><td>3.</td><td class="tdc"><u>Quantity A</u></td><td class="tdc"><u>Quantity B</u></td><td/></tr>
<tr><td/><td class="tdc"><img class="inline" src="images/337g.jpg"/></td>
       <td class="tdc"><img class="inline" src="images/337h.jpg"/></td>
       <td><img class="inline" src="images/337e.jpg"/></td></tr>
</tbody>
</table>
</body></html>
"""


def test_qc_synthesiser_binds_centered_info_to_next_question():
    """Q14 / Q15 root-cause regression: the prior synthesiser put one
    Q's centered-info into the NEXT Q's Quantity B slot. The new
    synthesiser must bind centered info to the *next* numbered Q,
    leave Quantity A/B as the actual cell values, and pull image cells
    through as '[img:NAME.jpg]' tokens."""
    soup = BeautifulSoup(_QC_TWO_TABLES_HTML, "html.parser")
    table = soup.find("table")
    nodes = [table]
    ol = ek._synthesise_qc_ol(
        nodes,
        soup=BeautifulSoup("<html><body></body></html>", "html.parser"),
    )
    assert ol is not None
    lis = ol.find_all("li")
    assert len(lis) == 3

    def _ps(li):
        return [p.get_text(" ", strip=True) for p in li.find_all("p")]

    # Q1: no centered info, plain text quantities, sup/sub preserved.
    q1_paragraphs = _ps(lis[0])
    assert q1_paragraphs[0] == "Quantity A: x^{2} + 2x − 2"
    assert q1_paragraphs[1] == "Quantity B: x^{2} + 2x − 1"

    # Q2: centered info ("x = 2y; y is a positive integer.") bound here.
    q2_paragraphs = _ps(lis[1])
    assert any("x = 2y; y is a positive integer." in p for p in q2_paragraphs)
    assert any(p.startswith("Quantity A: 4^{y}") for p in q2_paragraphs)
    # Quantity B was an <img>; it's now an <img class="inline"> child of the <p>.
    qb_p = next(p for p in lis[1].find_all("p")
                if p.get_text(" ", strip=True).startswith("Quantity B"))
    assert qb_p.find("img") is not None
    assert "337f.jpg" in qb_p.find("img").get("src")

    # Q3: centered info ("q, r, and s are positive numbers; qrs > 12.")
    q3_paragraphs = _ps(lis[2])
    assert any("qrs > 12" in p for p in q3_paragraphs)
    qa_p = next(p for p in lis[2].find_all("p")
                if p.get_text(" ", strip=True).startswith("Quantity A"))
    qb_p = next(p for p in lis[2].find_all("p")
                if p.get_text(" ", strip=True).startswith("Quantity B"))
    assert "337g.jpg" in qa_p.find("img").get("src")
    assert "337h.jpg" in qb_p.find("img").get("src")


# ── Defect (d) — \$ preserved inside math contexts ───────────────────


def test_normalise_latex_keeps_escaped_dollar_inside_math():
    """Q16 root-cause regression: ``normalise_latex`` previously
    collapsed every ``\\$`` to ``$``, which corrupted ``\\(\\frac{\\$75}{\\$750}\\)``
    by turning the inner ``$`` into LaTeX math-mode delimiters.

    The fix must keep ``\\$`` escaped inside ``\\(...\\)`` while still
    unescaping ``\\$`` outside math (where ``\\$`` is a JSON-escape
    artefact, not a real LaTeX dollar)."""
    inside = "the cost is \\(\\frac{\\$75}{\\$750}\\) per gallon"
    out = ek.normalise_latex(inside)
    assert "\\$75" in out
    assert "\\$750" in out
    # And no stray un-escaped $ inside the math block.
    math = re.search(r"\\\((.*)\\\)", out).group(1)
    assert "$" not in math.replace("\\$", "")

    # Outside math, plain ``\$`` from JSON-escape leftovers becomes ``$``.
    outside = "raw text with \\$5 only"
    assert ek.normalise_latex(outside) == "raw text with $5 only"


# ── Defect (e) — image-bucket classifier deterministic shortcuts ────


def test_image_classifier_drops_numeric_box_filename():
    """Q20 root-cause regression: 370a.jpg is the publisher's empty
    numeric-entry input glyph. Deterministic classifier must label it
    'numeric_box' so the figure-image gate drops it."""
    from services import image_classifier as ic
    assert ic.deterministic_classify("370a.jpg") == ic.BUCKET_NUMERIC_BOX


def test_image_classifier_drops_qc_bullet_glyph():
    from services import image_classifier as ic
    for src in ("a.jpg", "b.jpg", "37a.jpg", "37e.jpg",
                "s-a.jpg", "ga.jpg", "gb.jpg"):
        assert ic.deterministic_classify(src) == ic.BUCKET_BULLET, src


def test_image_classifier_returns_none_for_unknown_filename():
    """Filenames that don't match a deterministic rule must return
    None so the caller knows to fall back to vision."""
    from services import image_classifier as ic
    assert ic.deterministic_classify("325a.jpg") is None
    assert ic.deterministic_classify("368a.jpg") is None


# ── Defect (f) — TC multi-blank option relabelling ──────────────────


def test_relabel_multiblank_tc_2blank():
    """Defect 'f' regression: 6-option TC items with (i)/(ii) blank
    markers in the prompt get re-labelled blank1_A..blank2_C so the
    runtime renderer (which groups by `blank<N>_` prefix) shows them
    in two columns. Choice letters restart at A inside each blank,
    matching the convention in `scripts/seed_data.py` and the runtime
    renderer."""
    it = ek.RawItem(
        chapter_id="chapter05", section_title="", measure="verbal",
        subtype="tc", q_number=1,
        prompt="Mary believed that public service should (i) _________ science. Her contributions were (ii) _________ .",
        options=[
            ek.RawOption("A", "impede"),
            ek.RawOption("B", "replicate"),
            ek.RawOption("C", "outweigh", is_correct=True),
            ek.RawOption("D", "substantial", is_correct=True),
            ek.RawOption("E", "paltry"),
            ek.RawOption("F", "abhorrent"),
        ],
        correct_label="C, D",
    )
    ek._relabel_multiblank_tc_options(it)
    assert [o.label for o in it.options] == [
        "blank1_A", "blank1_B", "blank1_C",
        "blank2_A", "blank2_B", "blank2_C",
    ]
    assert it.correct_label == "blank1_C, blank2_A"


def test_relabel_multiblank_tc_3blank():
    it = ek.RawItem(
        chapter_id="chapter05", section_title="", measure="verbal",
        subtype="tc", q_number=6,
        prompt="(i) _________ Cuba was unpopular. He cemented the (ii) _________ of opponents. His party (iii) _________ him.",
        options=[
            ek.RawOption("A", "boycott"),
            ek.RawOption("B", "bolster"),
            ek.RawOption("C", "annex", is_correct=True),
            ek.RawOption("D", "enmity", is_correct=True),
            ek.RawOption("E", "approbation"),
            ek.RawOption("F", "largess"),
            ek.RawOption("G", "galvanize"),
            ek.RawOption("H", "abide"),
            ek.RawOption("I", "repudiate", is_correct=True),
        ],
        correct_label="C, D, I",
    )
    ek._relabel_multiblank_tc_options(it)
    assert [o.label for o in it.options] == [
        "blank1_A", "blank1_B", "blank1_C",
        "blank2_A", "blank2_B", "blank2_C",
        "blank3_A", "blank3_B", "blank3_C",
    ]
    assert it.correct_label == "blank1_C, blank2_A, blank3_C"


def test_relabel_multiblank_tc_leaves_singleblank_alone():
    """1-blank TC and SE items must keep flat A/B/C/D/E labels —
    relabel only kicks in when the prompt has (i)/(ii)/(iii) markers."""
    it = ek.RawItem(
        chapter_id="chapter05", section_title="", measure="verbal",
        subtype="tc", q_number=2,
        prompt="The intact specimen quest is one of the most _________ in marine biology.",
        options=[
            ek.RawOption("A", "meaningful"),
            ek.RawOption("B", "elusive", is_correct=True),
            ek.RawOption("C", "popular"),
            ek.RawOption("D", "expensive"),
            ek.RawOption("E", "profitable"),
        ],
        correct_label="B",
    )
    ek._relabel_multiblank_tc_options(it)
    assert [o.label for o in it.options] == ["A", "B", "C", "D", "E"]
    assert it.correct_label == "B"


# ── Defect (a) — trailing figure reattachment ──────────────────────


_OL_WITH_TRAILING_FIGURE = """
<ol class="ol0">
<li class="li-1">The area of a circle is 36. What is the circle's diameter?
<p class="txc"><img alt="image" class="inline" src="images/325a.jpg"/></p></li>
<li class="li-1">In the diagram above, what is the value of <i>a</i>?
<p class="tx1-1"><img alt="image" class="inline" src="images/325b.jpg"/></p></li>
</ol>
"""


def test_reattach_trailing_figures_moves_misplaced_figure():
    """Q9 root-cause regression: the publisher placed q4's diagram
    (`325a.jpg`) inside q3's <li>, and q4's stem opens with
    'In the diagram above'. The reattach helper must move 325a.jpg
    out of q3 and into q4."""
    soup = BeautifulSoup(_OL_WITH_TRAILING_FIGURE, "html.parser")
    ol = soup.find("ol")
    ek._reattach_trailing_figures(ol)
    lis = ol.find_all("li", recursive=False)
    q3_imgs = {(img.get("src") or "").rsplit("/", 1)[-1]
               for img in lis[0].find_all("img")}
    q4_imgs = {(img.get("src") or "").rsplit("/", 1)[-1]
               for img in lis[1].find_all("img")}
    # 325a.jpg should now live in q4 (which already had 325b.jpg).
    assert "325a.jpg" not in q3_imgs
    assert "325a.jpg" in q4_imgs
    assert "325b.jpg" in q4_imgs


def test_reattach_trailing_figures_leaves_legitimate_figures_alone():
    """If the current li actually says 'In the diagram below', do NOT
    move the figure to the next li (it really belongs to current)."""
    html = """
    <ol class="ol0">
    <li class="li-1">In the diagram below, what is the area?
    <p class="txc"><img alt="image" class="inline" src="images/X.jpg"/></p></li>
    <li class="li-1">In the diagram above, what is the perimeter?</li>
    </ol>
    """
    soup = BeautifulSoup(html, "html.parser")
    ol = soup.find("ol")
    ek._reattach_trailing_figures(ol)
    lis = ol.find_all("li", recursive=False)
    q1_imgs = {(img.get("src") or "").rsplit("/", 1)[-1]
               for img in lis[0].find_all("img")}
    q2_imgs = {(img.get("src") or "").rsplit("/", 1)[-1]
               for img in lis[1].find_all("img")}
    # X.jpg should stay in q1 because q1 references the diagram itself.
    assert "X.jpg" in q1_imgs
    assert "X.jpg" not in q2_imgs


def test_paragraph_lone_image_excludes_qc_quantity_paragraphs():
    """The lone-image detector must NEVER detect 'Quantity A: <img>'
    as a lone image (otherwise the QC quantity-image gets demoted to
    figure_image and Stage B never transcribes it)."""
    soup = BeautifulSoup(
        '<p class="tx1">Quantity A: <img class="inline" src="images/337g.jpg"/></p>',
        "html.parser",
    )
    p = soup.find("p")
    assert ek._is_paragraph_lone_image(p) is False

