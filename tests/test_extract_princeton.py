"""Unit tests for the deterministic Princeton extractor.

These tests use small, hand-crafted XHTML fragments (one per RC drill,
one per TC drill, one per quant drill, one per answer chapter) so they
do not depend on the EPUB being present at test time. The fragments
mirror the real markup conventions inspected during plan section 1 of
``.claude/plans/princeton-extraction.md``.
"""
import io
import zipfile
import pytest

from scripts.extract_princeton import (
    BULLET_IMAGE_FILES,
    PASSAGE_MARKER_CLASSES,
    clean_money_dollars, parse_numeric_value, latex_balance_check,
    normalise_option_text,
    parse_drill_chapter, parse_answer_chapter,
    detect_passages,
    attach_answer_keys, derive_correct_flags, numeric_answer_dict,
    run_validation_gates, GATE_NAMES, SUBTYPE_OPTION_COUNTS,
    extract_section,
    fraction_text_from_filename, is_transcribable_inline_gif,
    _detect_rc_subtype, _parse_chapter_path,
    _text_from, _option_text_from_img_hang_p,
    NUMERIC_ENTRY_BOX_FILES,
    reclassify_subtype_from_answer_key,
)
from bs4 import BeautifulSoup


# Pure helpers -----------------------------------------------------------


def test_clean_money_dollars_collapses_double_dollar_artefact():
    assert clean_money_dollars("$$5{,}000$") == "$5,000"
    assert clean_money_dollars("price is $$3$") == "price is $3"
    assert clean_money_dollars("no money here") == "no money here"


def test_parse_numeric_value_handles_common_forms():
    assert parse_numeric_value("7") == 7.0
    assert parse_numeric_value("4,800") == 4800.0
    assert parse_numeric_value("0.25") == 0.25
    assert parse_numeric_value("1/3") == pytest.approx(1 / 3)
    assert parse_numeric_value(r"\frac{1}{3}") == pytest.approx(1 / 3)
    assert parse_numeric_value(r"\sqrt{2}") == pytest.approx(2 ** 0.5)
    assert parse_numeric_value("7.5 gallons") == 7.5
    assert parse_numeric_value("") is None
    assert parse_numeric_value(None) is None


def test_latex_balance_check_passes_clean_text():
    ok, defects = latex_balance_check(r"x = \(\frac{1}{2}\)")
    assert ok and defects == []


def test_latex_balance_check_flags_unmatched_paren():
    ok, defects = latex_balance_check(r"x = \(\frac{1}{2}")
    assert not ok and "unmatched_paren" in defects


def test_latex_balance_check_flags_raw_backslash_f():
    ok, defects = latex_balance_check(r"hello \f world")
    assert not ok and "raw_backslash_f" in defects
    ok, _ = latex_balance_check(r"\(\frac{1}{2}\) \(\fbox{a}\)")
    assert ok


def test_latex_balance_check_flags_bare_dollar_amount():
    # Prose dollar amounts (e.g. ``$5 million`` in a word problem) are NOT
    # bare LaTeX delimiters and must not be flagged. The check only fires
    # when a $ is immediately followed by a LaTeX command — that's the
    # actual broken-render shape (``$\frac{1}{2}$`` etc.).
    ok, _ = latex_balance_check("price is $5")
    assert ok
    ok, _ = latex_balance_check("$5.4 million in 2004")
    assert ok
    ok, defects = latex_balance_check(r"answer is $\frac{1}{2}")
    assert not ok and "bare_dollar" in defects
    ok, _ = latex_balance_check(r"price is \$5")
    assert ok


def test_normalise_option_text_collapses_whitespace_and_lowercases():
    assert normalise_option_text("Hello   WORLD\n") == "hello world"


# Section path parsing ---------------------------------------------------


def test_parse_chapter_path_handles_drill_slug():
    spec = _parse_chapter_path(
        "OEBPS/Revi_9780307945396_epub_c02_s03_tcd1_r1.htm"
    )
    assert spec["role"] == "drill"
    assert spec["measure"] == "verbal"
    assert spec["base_slug"] == "tcd"
    assert spec["drill_num"] == 1
    assert spec["chapter"] == "02"


def test_parse_chapter_path_handles_answer_slug():
    spec = _parse_chapter_path(
        "OEBPS/Revi_9780307945396_epub_c02_s10_tcAnE_r1.htm"
    )
    assert spec["role"] == "answers"
    assert spec["measure"] == "verbal"


def test_parse_chapter_path_returns_none_for_unrelated_files():
    assert _parse_chapter_path("OEBPS/cover.html") is None


# RC subtype detection ---------------------------------------------------


def test_rc_subtype_select_passage_takes_priority():
    assert _detect_rc_subtype(
        "Select the sentence that best supports the author's claim."
    ) == "rc_select_passage"


def test_rc_subtype_multi_matches_consider_each_variants():
    assert _detect_rc_subtype(
        "Consider each of the choices separately. The passage suggests..."
    ) == "rc_multi"
    assert _detect_rc_subtype(
        "Consider each of the following answer choices separately..."
    ) == "rc_multi"
    assert _detect_rc_subtype(
        "Select all that apply. Which of these..."
    ) == "rc_multi"


def test_rc_subtype_default_is_rc_single():
    assert _detect_rc_subtype("What does the author imply?") == "rc_single"


# In-memory EPUB fixture builder ----------------------------------------


def _make_epub(files):
    """Build an in-memory ZipFile that looks like a Princeton EPUB."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


# A minimal TC drill chapter
_TCD1_HTML = """
<html><body>
<h2 class="section_pagebreak"><strong>DRILL 1</strong></h2>
<p class="extract1" id="QST41">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c02_s10_tcAnE_r1.htm#QST41a">1</a>
</p>
<div class="block0">
  <p class="nonindent">A short stem with a single _______.</p>
  <p class="center">
    <img alt="" height="108"
         src="images/Revi_9780307945396_fi006_r1.gif" width="182"/>
  </p>
</div>
<p class="extract1" id="QST42">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c02_s10_tcAnE_r1.htm#QST42a">2</a>
</p>
<div class="block0">
  <p class="nonindent">Two-blank stem (i)____ and (ii)____.</p>
  <p class="center">
    <img alt="" height="88"
         src="images/Revi_9780307945396_fi007_r1.gif" width="272"/>
  </p>
</div>
</body></html>
"""

_TC_AN_HTML = """
<html><body>
<h2 class="section"><strong>ANSWERS</strong></h2>
<h3 class="section"><strong>Drill 1</strong></h3>
<div class="hanging0">
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c02_s03_tcd1_r1.htm#QST41">1.</a>
    E
  </p>
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c02_s03_tcd1_r1.htm#QST42">2.</a>
    initiative, strive
  </p>
</div>
</body></html>
"""

_RCD1_HTML = """
<html><body>
<h2 class="section_pagebreak"><strong>DRILL 1</strong></h2>
<p class="extract1">Questions 1-2 refer to the following passage.</p>
<div class="block_rc">
  <p class="read_comp">A short two-question passage about test prep.</p>
</div>
<p class="extract1" id="QST146">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c02_s18_rcAnE_r1.htm#QST146a">1</a>
</p>
<div class="block0">
  <p class="nonindent">The passage primarily concerns</p>
</div>
<div class="img_hang">
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     test prep
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     biology
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     history
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     economics
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     literature
  </p>
</div>
<p class="extract1" id="QST147">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c02_s18_rcAnE_r1.htm#QST147a">2</a>
</p>
<div class="block0">
  <p class="nonindent"><em>Consider each of the choices separately and
     select all that apply.</em></p>
  <p class="extract">The passage suggests that test prep is</p>
</div>
<div class="img_hang">
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_420_r1.jpg" width="14"/>
     valuable
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_420_r1.jpg" width="14"/>
     time-consuming
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_420_r1.jpg" width="14"/>
     expensive
  </p>
</div>
</body></html>
"""

_RC_AN_HTML = """
<html><body>
<h2 class="section"><strong>ANSWERS</strong></h2>
<h3 class="section"><strong>Drill 1</strong></h3>
<div class="hanging0">
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c02_s12_rcd1_r1.htm#QST146">1.</a>
    A
  </p>
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c02_s12_rcd1_r1.htm#QST147">2.</a>
    A, C
  </p>
</div>
</body></html>
"""

_PID1_HTML = """
<html><body>
<h2 class="section"><strong>DRILL 1</strong></h2>
<p class="extract1" id="QST377">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c03_s06_piAnE_r1.htm#QST377a">1</a>
</p>
<div class="block0">
  <p class="nonindent">Profit formula 4y - 2.</p>
  <table border="0" cellspacing="0" width="100%">
    <tr valign="top">
      <td align="center"><span class="underline">Quantity A</span></td>
      <td align="center"><span class="underline">Quantity B</span></td>
    </tr>
    <tr valign="top">
      <td align="center">4 times the profit</td>
      <td align="center">16y - 4</td>
    </tr>
  </table>
</div>
<div class="img_hang">
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     Quantity A is greater.
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     Quantity B is greater.
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     The two quantities are equal.
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     The relationship cannot be determined.
  </p>
</div>
<p class="extract1" id="QST378">Question
  <a class="hlink"
     href="Revi_9780307945396_epub_c03_s06_piAnE_r1.htm#QST378a">2</a>
</p>
<div class="block0">
  <p class="nonindent">Pick a number between 1 and 10.</p>
</div>
<div class="img_hang">
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     2
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     3
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     5
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     7
  </p>
  <p class="img_hang"><img alt="" height="14"
     src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
     9
  </p>
</div>
</body></html>
"""

_PI_AN_HTML = """
<html><body>
<h3 class="section"><strong>Drill 1</strong></h3>
<div class="hanging0">
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c03_s02_pid1_r1.htm#QST377">1.</a>
    C
  </p>
  <p class="hanging0">
    <a class="hlink"
       href="Revi_9780307945396_epub_c03_s02_pid1_r1.htm#QST378">2.</a>
    B
  </p>
</div>
</body></html>
"""


@pytest.fixture
def fake_epub():
    return _make_epub({
        "OEBPS/Revi_9780307945396_epub_c02_s03_tcd1_r1.htm": _TCD1_HTML,
        "OEBPS/Revi_9780307945396_epub_c02_s10_tcAnE_r1.htm": _TC_AN_HTML,
        "OEBPS/Revi_9780307945396_epub_c02_s12_rcd1_r1.htm": _RCD1_HTML,
        "OEBPS/Revi_9780307945396_epub_c02_s18_rcAnE_r1.htm": _RC_AN_HTML,
        "OEBPS/Revi_9780307945396_epub_c03_s02_pid1_r1.htm": _PID1_HTML,
        "OEBPS/Revi_9780307945396_epub_c03_s06_piAnE_r1.htm": _PI_AN_HTML,
    })


# Stage A — drill / answer chapter parsing -------------------------------


def test_parse_answer_chapter_extracts_label_per_qst_id(fake_epub):
    answers = parse_answer_chapter(
        fake_epub, "OEBPS/Revi_9780307945396_epub_c02_s10_tcAnE_r1.htm")
    assert answers == {41: "E", 42: "initiative, strive"}


def test_parse_drill_chapter_tc_marks_needs_vision(fake_epub):
    qs = parse_drill_chapter(
        fake_epub, "OEBPS/Revi_9780307945396_epub_c02_s03_tcd1_r1.htm")
    assert len(qs) == 2
    assert all(q["subtype"] == "tc" for q in qs)
    assert all(q["needs_vision"] is True for q in qs)
    for q in qs:
        for ref in q["figure_refs"]:
            assert ref["filename"] not in BULLET_IMAGE_FILES
    assert "single _______" in qs[0]["prompt"]
    assert "(i)____" in qs[1]["prompt"]


def test_parse_drill_chapter_rc_clusters_two_questions_to_one_passage(
        fake_epub):
    qs = parse_drill_chapter(
        fake_epub, "OEBPS/Revi_9780307945396_epub_c02_s12_rcd1_r1.htm")
    assert len(qs) == 2
    assert qs[0]["stimulus_anchor"] == qs[1]["stimulus_anchor"]
    assert qs[0]["stimulus_anchor"]
    assert qs[0]["subtype"] == "rc_single"
    assert qs[1]["subtype"] == "rc_multi"
    assert len(qs[0]["options"]) == 5
    assert len(qs[1]["options"]) == 3
    assert all(not q["needs_vision"] for q in qs)


def test_parse_drill_chapter_qc_detects_quantity_table(fake_epub):
    qs = parse_drill_chapter(
        fake_epub, "OEBPS/Revi_9780307945396_epub_c03_s02_pid1_r1.htm")
    qc_q = next(q for q in qs if q["qst_id"] == 377)
    assert qc_q["subtype"] == "qc"
    assert "Quantity A" in qc_q["prompt"]
    assert "Quantity B" in qc_q["prompt"]
    assert len(qc_q["options"]) == 4


def test_parse_drill_chapter_quant_mcq_single(fake_epub):
    qs = parse_drill_chapter(
        fake_epub, "OEBPS/Revi_9780307945396_epub_c03_s02_pid1_r1.htm")
    mcq = next(q for q in qs if q["qst_id"] == 378)
    assert mcq["subtype"] == "mcq_single"
    assert len(mcq["options"]) == 5


# Passage detection ------------------------------------------------------


def test_detect_passages_yields_one_entry_per_passage_marker():
    soup = BeautifulSoup(_RCD1_HTML, "html.parser")
    passages = detect_passages(soup)
    assert len(passages) == 1
    assert passages[0]["q_start"] == 1 and passages[0]["q_end"] == 2
    assert "test prep" in passages[0]["passage_text"]


# Stage C — answer-key wiring -------------------------------------------


def test_attach_answer_keys_pairs_by_qst_id():
    qs = [{"qst_id": 41, "options": []}, {"qst_id": 42, "options": []}]
    paired = attach_answer_keys(qs, {41: "E", 42: "B"})
    assert paired == 2
    assert qs[0]["correct_label"] == "E"
    assert qs[1]["correct_label"] == "B"


def test_derive_correct_flags_for_letter_answer():
    item = {
        "correct_label": "C",
        "options": [
            {"label": "A", "text": "x", "is_correct": False},
            {"label": "B", "text": "y", "is_correct": False},
            {"label": "C", "text": "z", "is_correct": False},
        ],
    }
    derive_correct_flags(item)
    assert [o["is_correct"] for o in item["options"]] == [False, False, True]


def test_derive_correct_flags_for_letter_pair_answer():
    item = {
        "correct_label": "A, C",
        "options": [
            {"label": "A", "text": "x", "is_correct": False},
            {"label": "B", "text": "y", "is_correct": False},
            {"label": "C", "text": "z", "is_correct": False},
        ],
    }
    derive_correct_flags(item)
    assert [o["is_correct"] for o in item["options"]] == [True, False, True]


def test_numeric_answer_dict_handles_fraction_and_decimal():
    assert numeric_answer_dict({"correct_label": "1/3"}) == \
        {"numerator": 1, "denominator": 3, "mode": "fraction"}
    assert numeric_answer_dict({"correct_label": "7"}) == \
        {"exact_value": 7.0, "mode": "decimal"}
    assert numeric_answer_dict({"correct_label": ""}) is None


# Stage D — validation gates --------------------------------------------


def _base_item(**kw):
    item = {
        "qst_id": 1, "drill_num": 1, "question_num": 1,
        "subtype": "mcq_single", "measure": "quant",
        "prompt": "What is 1+1? " * 3,
        "options": [
            {"label": "A", "text": "1", "is_correct": False},
            {"label": "B", "text": "2", "is_correct": True},
            {"label": "C", "text": "3", "is_correct": False},
            {"label": "D", "text": "4", "is_correct": False},
            {"label": "E", "text": "5", "is_correct": False},
        ],
        "correct_label": "B", "needs_vision": False,
        "stimulus_text": "", "stimulus_anchor": "",
        "figure_refs": [], "explanation": "",
    }
    item.update(kw)
    return item


def test_all_gates_pass_for_clean_mcq_single():
    item = _base_item()
    verdict = run_validation_gates(item)
    assert verdict["passed"], verdict["failed_gates"]


def test_g1_option_count_fails_on_wrong_count():
    item = _base_item(options=[{"label": "A", "text": "1",
                                "is_correct": True}])
    verdict = run_validation_gates(item)
    assert "G1_option_count" in verdict["failed_gates"]


def test_g2_distractor_unique_fails_on_dup_text():
    item = _base_item(options=[
        {"label": "A", "text": "same", "is_correct": True},
        {"label": "B", "text": "same", "is_correct": False},
        {"label": "C", "text": "x", "is_correct": False},
        {"label": "D", "text": "y", "is_correct": False},
        {"label": "E", "text": "z", "is_correct": False},
    ])
    verdict = run_validation_gates(item)
    assert "G2_distractor_unique" in verdict["failed_gates"]


def test_g3_correct_count_fails_when_two_marked_for_single():
    item = _base_item()
    item["options"][0]["is_correct"] = True
    verdict = run_validation_gates(item)
    assert "G3_correct_count" in verdict["failed_gates"]


def test_g4_answer_key_paired_fails_when_label_missing():
    item = _base_item(correct_label=None)
    for o in item["options"]:
        o["is_correct"] = False
    verdict = run_validation_gates(item)
    assert "G4_answer_key_paired" in verdict["failed_gates"]


def test_g5_latex_fails_on_unmatched_paren():
    item = _base_item(prompt=r"compute \(\frac{1}{2}" + " padding " * 10)
    verdict = run_validation_gates(item)
    assert "G5_latex_well_formed" in verdict["failed_gates"]


def test_g6_rc_cluster_fails_when_passage_missing():
    item = _base_item(subtype="rc_single", measure="verbal",
                      stimulus_anchor="")
    verdict = run_validation_gates(item)
    assert "G6_rc_cluster_coherent" in verdict["failed_gates"]


def test_g7_numeric_passes_for_simple_int():
    item = _base_item(subtype="numeric_entry", options=[],
                      correct_label="42")
    verdict = run_validation_gates(item)
    assert "G7_numeric_parseable" not in verdict["failed_gates"]


def test_g7_numeric_fails_when_unparseable():
    item = _base_item(subtype="numeric_entry", options=[],
                      correct_label="please see the appendix")
    verdict = run_validation_gates(item)
    assert "G7_numeric_parseable" in verdict["failed_gates"]


def test_g8_figure_attached_passes_when_no_figure():
    item = _base_item(figure_refs=[])
    verdict = run_validation_gates(item)
    assert "G8_figure_attached_when_referenced" not in verdict["failed_gates"]


def test_g8_figure_attached_passes_with_filename():
    item = _base_item(figure_refs=[{"filename": "x.gif"}])
    verdict = run_validation_gates(item)
    assert "G8_figure_attached_when_referenced" not in verdict["failed_gates"]


def test_g9_qc_quantity_labels_fails_when_missing():
    item = _base_item(subtype="qc", options=[
        {"label": "A", "text": "x", "is_correct": True},
        {"label": "B", "text": "y", "is_correct": False},
        {"label": "C", "text": "z", "is_correct": False},
        {"label": "D", "text": "w", "is_correct": False},
    ], correct_label="A")
    item["prompt"] = "What is bigger? " * 3
    verdict = run_validation_gates(item)
    assert "G9_qc_quantity_labels" in verdict["failed_gates"]


def test_g10_prompt_length_fails_on_too_short():
    item = _base_item(prompt="hi")
    verdict = run_validation_gates(item)
    assert "G10_prompt_length_sane" in verdict["failed_gates"]


def test_g11_single_correct_fails_when_two_marked():
    item = _base_item()
    item["options"][2]["is_correct"] = True
    verdict = run_validation_gates(item)
    assert "G11_single_correct_adversarial" in verdict["failed_gates"]


def test_g12_dedup_against_existing_drops_duplicates():
    item = _base_item()
    existing = {item["prompt"][:120].lower().strip(): True}
    verdict = run_validation_gates(item, existing_prefixes=existing)
    assert "G12_dedup_against_existing" in verdict["failed_gates"]


def test_gate_names_all_have_handlers():
    item = _base_item()
    verdict = run_validation_gates(item)
    assert set(verdict["details"].keys()) == set(GATE_NAMES)


# End-to-end on the fake EPUB -------------------------------------------


def test_extract_section_end_to_end_on_fake_epub(tmp_path):
    epub_path = str(tmp_path / "fake.epub")
    files = {
        "OEBPS/Revi_9780307945396_epub_c02_s12_rcd1_r1.htm": _RCD1_HTML,
        "OEBPS/Revi_9780307945396_epub_c02_s18_rcAnE_r1.htm": _RC_AN_HTML,
    }
    with zipfile.ZipFile(epub_path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)

    questions, answer_map = extract_section(epub_path, "rcd")
    assert len(questions) == 2
    assert answer_map == {146: "A", 147: "A, C"}
    q1, q2 = questions
    assert q1["correct_label"] == "A"
    assert q2["correct_label"] == "A, C"
    assert sum(o["is_correct"] for o in q1["options"]) == 1
    assert sum(o["is_correct"] for o in q2["options"]) == 2
    assert q1["stimulus_anchor"] == q2["stimulus_anchor"]


# Subtype option-count table sanity --------------------------------------


def test_subtype_option_counts_table_covers_known_subtypes():
    expected_subtypes = {
        "mcq_single", "mcq_multi", "qc", "tc", "se",
        "rc_single", "rc_multi", "rc_select_passage",
        "data_interp", "numeric_entry",
    }
    assert set(SUBTYPE_OPTION_COUNTS) == expected_subtypes


# Defect-fix tests (post-review) ----------------------------------------


def test_text_from_preserves_superscripts():
    """``<sup>`` should render as ``^{...}`` so we don't lose exponents."""
    soup = BeautifulSoup(
        "<p>9<em>p</em><sup class=\"frac\">2</sup></p>", "html.parser")
    assert _text_from(soup.p) == "9p^{2}"


def test_text_from_preserves_subscripts():
    soup = BeautifulSoup(
        "<p>x<sub>1</sub> + x<sub>2</sub></p>", "html.parser")
    assert _text_from(soup.p) == "x_{1} + x_{2}"


def test_text_from_handles_nested_sup_sub():
    soup = BeautifulSoup(
        "<p>a<sup>b<sub>c</sub></sup></p>", "html.parser")
    # nested: a^{b_{c}}
    assert _text_from(soup.p) == "a^{b_{c}}"


def test_text_from_replaces_fraction_gif_inline():
    soup = BeautifulSoup(
        '<p>x = <img class="inline" src="images/Revi_9780307945396_epub_frac4-7_r1.gif"/></p>',
        "html.parser")
    assert _text_from(soup.p) == "x = (4/7)"


def test_fraction_text_from_filename_round_trip():
    assert fraction_text_from_filename("Revi_9780307945396_epub_frac4-7_r1.gif") == "(4/7)"
    assert fraction_text_from_filename("Revi_9780307945396_epub_frac3-10_r1.gif") == "(3/10)"
    assert fraction_text_from_filename("not_a_fraction.gif") is None
    assert fraction_text_from_filename(None) is None
    assert fraction_text_from_filename("") is None


def test_is_transcribable_inline_gif():
    # Generic publisher inline glyph (operator def) → True.
    assert is_transcribable_inline_gif("Revi_9780307945396_epub_1236_r1.gif")
    # Fraction GIF → False (already handled deterministically).
    assert not is_transcribable_inline_gif(
        "Revi_9780307945396_epub_frac4-7_r1.gif")
    # Bullet → False.
    for f in BULLET_IMAGE_FILES:
        assert not is_transcribable_inline_gif(f)
    # JPG figure → False (must be a *.gif).
    assert not is_transcribable_inline_gif("Revi_9780307945396_epub_975_r1.jpg")


def test_passage_marker_classes_includes_di_classes():
    # The DI chapter cgd1 uses these classes — verify the constant is wired
    # so the extractor recognises them as cluster boundaries.
    for cls in ("nonindent_pagebreak", "extract_pagebreak", "extract1",
                "extract1_pagebreak", "nonindent"):
        assert cls in PASSAGE_MARKER_CLASSES


def test_option_text_from_img_hang_p_replaces_fraction_gif():
    html = ('<p class="img_hang">'
            '<img alt="" height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>'
            '<img alt="" class="inline" src="images/Revi_9780307945396_epub_frac4-7_r1.gif"/>'
            '</p>')
    soup = BeautifulSoup(html, "html.parser")
    assert _option_text_from_img_hang_p(soup.p) == "(4/7)"


def test_option_text_from_img_hang_p_keeps_placeholder_for_unknown_gif():
    html = ('<p class="img_hang">'
            '<img alt="" height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>'
            '<img alt="" class="inline" src="images/Revi_9780307945396_epub_1046_r1.gif"/>'
            '</p>')
    soup = BeautifulSoup(html, "html.parser")
    text = _option_text_from_img_hang_p(soup.p)
    assert "[img:Revi_9780307945396_epub_1046_r1.gif]" in text


# DI cluster detection (regression for defect e) ------------------------


_CGD_CLUSTER_HTML = """
<html><body>
<h2 class="section">DRILL 1</h2>
<p class="nonindent">Questions 1-3 refer to the following data.</p>
<p class="center">
  <img alt="" height="445" src="images/Revi_9780307945396_epub_974_r1.jpg" width="414"/>
</p>
<p class="nonindent" id="QST789">Question
  <a class="hlink" href="x.htm#QST789a">1</a></p>
<div class="block0"><p class="nonindent">Stem of question 1.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>1</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>2</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>3</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>4</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>5</p>
</div>
<p class="nonindent" id="QST790">Question
  <a class="hlink" href="x.htm#QST790a">2</a></p>
<div class="block0"><p class="nonindent">Stem of question 2.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>a</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>b</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>c</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>d</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>e</p>
</div>
<p class="nonindent" id="QST791">Question
  <a class="hlink" href="x.htm#QST791a">3</a></p>
<div class="block0"><p class="nonindent">Stem of question 3.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>x</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>y</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>z</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>w</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>v</p>
</div>
<hr/>
<p class="nonindent_pagebreak">Questions 4-5 refer to the following data.</p>
<div class="block0">
  <p class="center"><img alt="" height="600" src="images/Revi_9780307945396_epub_975_r1.jpg" width="500"/></p>
  <p class="caption">(Click <a class="hlink" href="x.htm">here</a> to view a larger image.)</p>
</div>
<p class="nonindent" id="QST792">Question
  <a class="hlink" href="x.htm#QST792a">4</a></p>
<div class="block0"><p class="nonindent">Stem of question 4.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>1</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>2</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>3</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>4</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>5</p>
</div>
<p class="nonindent" id="QST793">Question
  <a class="hlink" href="x.htm#QST793a">5</a></p>
<div class="block0"><p class="nonindent">Stem of question 5.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>1</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>2</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>3</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>4</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>5</p>
</div>
</body></html>
"""


def test_di_cluster_attaches_chart_to_every_sibling(tmp_path):
    """Regression for defect (e): cgd1 Q14 (qst791) was orphaned from its
    chart. After the fix, every question in the cluster shares one
    ``stimulus_anchor`` and the chart appears in every sibling's
    ``figure_refs``."""
    epub_path = str(tmp_path / "fake.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s52_cgd1_r1.htm",
                   _CGD_CLUSTER_HTML)
    qs, _ = extract_section(epub_path, "cgd")
    assert len(qs) == 5
    cluster1 = [q for q in qs if q["qst_id"] in (789, 790, 791)]
    cluster2 = [q for q in qs if q["qst_id"] in (792, 793)]

    # Every member of cluster 1 shares one anchor.
    anchors1 = {q["stimulus_anchor"] for q in cluster1}
    assert len(anchors1) == 1, anchors1
    assert anchors1 != {""}, "anchor should be populated"
    # Anchor encodes drill+passage range.
    assert "q1-3" in next(iter(anchors1))

    # Every cluster1 question has the chart attached.
    for q in cluster1:
        fnames = {f["filename"] for f in q["figure_refs"]}
        assert "Revi_9780307945396_epub_974_r1.jpg" in fnames, q["qst_id"]

    # Cluster 2 anchor differs and has its own chart.
    anchors2 = {q["stimulus_anchor"] for q in cluster2}
    assert len(anchors2) == 1
    assert anchors2 != anchors1
    for q in cluster2:
        fnames = {f["filename"] for f in q["figure_refs"]}
        assert "Revi_9780307945396_epub_975_r1.jpg" in fnames, q["qst_id"]


def test_di_cluster_marker_does_not_leak_into_question_stem(tmp_path):
    """qst791 stem must NOT contain 'Questions 4-5 refer' or chart caption."""
    epub_path = str(tmp_path / "fake2.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s52_cgd1_r1.htm",
                   _CGD_CLUSTER_HTML)
    qs, _ = extract_section(epub_path, "cgd")
    q791 = next(q for q in qs if q["qst_id"] == 791)
    assert "Questions 4-5" not in q791["prompt"]
    assert "view a larger image" not in q791["prompt"]
    assert q791["prompt"].strip().startswith("Stem of question 3.")


# QC stem de-duplication (regression for QC defect) ---------------------


_QC_DEDUP_HTML = """
<html><body>
<h2>DRILL 1</h2>
<p class="extract1" id="QST878">Question
  <a class="hlink" href="x.htm#QST878a">1</a></p>
<div class="block0">
  <table border="0" cellspacing="0" width="100%">
    <tr valign="top">
      <td align="center"><span class="underline">Quantity A</span></td>
      <td align="center"><span class="underline">Quantity B</span></td>
    </tr>
    <tr valign="top">
      <td align="center">(3<em>p</em> + 1)(3<em>p</em> – 1)</td>
      <td align="center">9<em>p</em><sup class="frac">2</sup></td>
    </tr>
  </table>
</div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>Quantity A is greater.</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>Quantity B is greater.</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>The two quantities are equal.</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>The relationship cannot be determined from the information given.</p>
</div>
</body></html>
"""


def test_qc_stem_does_not_duplicate_quantity_values(tmp_path):
    epub_path = str(tmp_path / "qc.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s62_qed1_r1.htm",
                   _QC_DEDUP_HTML)
    qs, _ = extract_section(epub_path, "qed")
    assert len(qs) == 1
    q = qs[0]
    # Quantity values should appear exactly once in the prompt — not twice
    # (once flat as `(3p + 1)(3p - 1) 9p^{2}` and once as the labelled form).
    prompt = q["prompt"]
    assert prompt.count("(3p + 1)") == 1, prompt
    # Superscript was preserved.
    assert "9p^{2}" in prompt or "9p^{2}\n" in prompt
    # Labelled form is present.
    assert "Quantity A:" in prompt
    assert "Quantity B:" in prompt
    # And the prompt doesn't start with a stray blank line.
    assert prompt == prompt.strip()


# Inline-GIF target tracking (defect c, d) ------------------------------


_INLINE_GIF_HTML = """
<html><body>
<h2>DRILL 1</h2>
<p class="extract1" id="QST847">Question
  <a class="hlink" href="x.htm#QST847a">1</a></p>
<div class="block0"><p class="nonindent">Compute.</p></div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>
    <img class="inline" height="38" src="images/Revi_9780307945396_epub_1046_r1.gif" width="21"/></p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>4</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>5</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>13</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>22</p>
</div>
</body></html>
"""


def test_option_inline_gif_flagged_for_vision(tmp_path):
    epub_path = str(tmp_path / "inline.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s58_led1_r1.htm",
                   _INLINE_GIF_HTML)
    qs, _ = extract_section(epub_path, "led")
    assert len(qs) == 1
    q = qs[0]
    targets = q.get("inline_gif_targets") or []
    assert any(t["filename"] == "Revi_9780307945396_epub_1046_r1.gif"
               and t["context"] == "option"
               for t in targets), targets
    assert q["needs_vision"] is True


def test_inline_gif_in_stem_does_not_create_diagram_figure(tmp_path):
    """An inline operator-def GIF should be tracked as ``inline_gif_targets``,
    not as a regular ``figure_refs`` diagram (defect d, qst952)."""
    html = """
<html><body>
<h2>DRILL 1</h2>
<p class="extract1" id="QST952">Question
  <a class="hlink" href="x.htm#QST952a">1</a></p>
<div class="block0">
  <p class="nonindent">x op y = <img alt="" height="24" src="images/Revi_9780307945396_epub_1236_r1.gif" width="31"/>.</p>
</div>
<div class="img_hang">
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>1</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>2</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>3</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>4</p>
  <p class="img_hang"><img height="14" src="images/Revi_9780307945396_epub_421_r1.jpg" width="14"/>5</p>
</div>
</body></html>
"""
    epub_path = str(tmp_path / "op.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s72_gsfd1_r1.htm", html)
    qs, _ = extract_section(epub_path, "gsfd")
    assert len(qs) == 1
    q = qs[0]
    # The 1236 GIF must NOT show up as a figure_refs entry (it's inline math).
    diag_fnames = {f["filename"] for f in q.get("figure_refs", [])}
    assert "Revi_9780307945396_epub_1236_r1.gif" not in diag_fnames
    # It MUST appear in inline_gif_targets so vision can transcribe it.
    targets = {t["filename"] for t in q.get("inline_gif_targets", [])}
    assert "Revi_9780307945396_epub_1236_r1.gif" in targets
    assert q["needs_vision"] is True


# Regression tests for the missing-options defect class -----------------


def test_numeric_entry_box_files_includes_351_and_112():
    """The user-reported "missing options" bug class. Without 351 / 112
    in this set, the parser misclassified ~30 numeric-entry questions as
    mcq_single with zero options."""
    assert "Revi_9780307945396_epub_351_r1.jpg" in NUMERIC_ENTRY_BOX_FILES
    assert "Revi_9780307945396_epub_112_r1.jpg" in NUMERIC_ENTRY_BOX_FILES


def test_extract_numeric_entry_with_351_box(tmp_path):
    """A drill question whose only option-area artwork is the 351 box
    must be typed as numeric_entry, NOT as mcq_single with zero options."""
    html = """<html><body>
<p class="extract" id="QST982">Question 10</p>
<div class="block0">
  <p class="nonindent">Six students compete in a tournament. How many games?</p>
  <p class="center">
    <img height="26" src="images/Revi_9780307945396_epub_351_r1.jpg" width="78"/>
  </p>
</div>
<p class="extract" id="QST983">Question 11</p>
<div class="block0"><p class="nonindent">filler</p></div>
</body></html>
"""
    epub_path = str(tmp_path / "ne.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s76_cpd1_r1.htm", html)
        # Minimal answer chapter so attach_answer_keys finds a numeric label.
        z.writestr(
            "OEBPS/Revi_9780307945396_epub_c03_s78_cpAnE_r1.htm",
            '<html><body>'
            '<p class="hanging0"><a class="hlink" '
            'href="Revi_9780307945396_epub_c03_s76_cpd1_r1.htm#QST982">10.</a> 60</p>'
            '</body></html>'
        )
    qs, _ = extract_section(epub_path, "cpd")
    q = next(q for q in qs if q["qst_id"] == 982)
    assert q["subtype"] == "numeric_entry"
    assert q["options"] == []
    # The 351 box should NOT have leaked into figure_refs.
    assert all(fr.get("filename") != "Revi_9780307945396_epub_351_r1.jpg"
               for fr in q.get("figure_refs") or [])
    assert q.get("has_numeric_box") is True


def test_extract_numeric_entry_with_112_fraction_box(tmp_path):
    """Same as above but with the 108x60 stacked fraction-entry box."""
    html = """<html><body>
<p class="extract" id="QST462">Question 5</p>
<div class="block0">
  <p class="nonindent_tall">Betty sold three-fifths. What fraction?</p>
  <p class="center">
    <img height="60" src="images/Revi_9780307945396_epub_112_r1.jpg" width="108"/>
  </p>
</div>
</body></html>
"""
    epub_path = str(tmp_path / "ne2.epub")
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("OEBPS/Revi_9780307945396_epub_c03_s09_phd3_r1.htm", html)
        z.writestr(
            "OEBPS/Revi_9780307945396_epub_c03_s10_phAnE_r1.htm",
            '<html><body>'
            '<p class="hanging0"><a class="hlink" '
            'href="Revi_9780307945396_epub_c03_s09_phd3_r1.htm#QST462">5.</a> 1/5</p>'
            '</body></html>'
        )
    qs, _ = extract_section(epub_path, "phd")
    q = next(q for q in qs if q["qst_id"] == 462)
    assert q["subtype"] == "numeric_entry"
    assert q["options"] == []
    assert all(fr.get("filename") != "Revi_9780307945396_epub_112_r1.jpg"
               for fr in q.get("figure_refs") or [])
    # Numeric answer dict should have been derived
    na = q.get("numeric_answer")
    assert na is not None
    assert na.get("mode") == "fraction"
    assert na.get("numerator") == 1
    assert na.get("denominator") == 5


def test_reclassify_multi_letter_answer_key_to_mcq_multi():
    """A single-letter answer like 'B, E' must promote subtype to mcq_multi."""
    q = {
        "qst_id": 428, "subtype": "mcq_single", "base_slug": "pid",
        "prompt": "Which of the following values of x satisfy the equation?",
        "options": [
            {"label": l, "text": str(i), "is_correct": False}
            for i, l in enumerate("ABCDEFG")
        ],
        "correct_label": "B, E",
    }
    new = reclassify_subtype_from_answer_key(q)
    assert new == "mcq_multi"
    flags = {o["label"]: o["is_correct"] for o in q["options"]}
    assert flags["B"] is True and flags["E"] is True
    assert flags["A"] is False and flags["G"] is False


def test_reclassify_qc_options_to_qc_subtype():
    """Stem-as-image QC: options match the four QC boilerplate phrases."""
    q = {
        "qst_id": 526, "subtype": "mcq_single", "base_slug": "fdpd",
        "prompt": "",  # stem image
        "options": [
            {"label": "A", "text": "Quantity A is greater.", "is_correct": False},
            {"label": "B", "text": "Quantity B is greater.", "is_correct": True},
            {"label": "C", "text": "The two quantities are equal.", "is_correct": False},
            {"label": "D", "text": "The relationship cannot be determined from the information given.",
             "is_correct": False},
        ],
        "correct_label": "B",
    }
    new = reclassify_subtype_from_answer_key(q)
    assert new == "qc"


def test_reclassify_numeric_answer_to_numeric_entry():
    """Numeric / fraction / dollar answer key + zero/wrong options ->
    numeric_entry. Must not flip when the question already has 5 well-
    formed MCQ options (the answer is just a numeric value among them)."""
    # Case 1: zero options + numeric label -> flip.
    q1 = {"qst_id": 982, "subtype": "mcq_single", "base_slug": "cpd",
          "prompt": "x", "options": [], "correct_label": "60",
          "figure_refs": [{"filename": "Revi_9780307945396_epub_351_r1.jpg"}]}
    assert reclassify_subtype_from_answer_key(q1) == "numeric_entry"
    assert q1["figure_refs"] == []

    # Case 2: 5 well-formed options + numeric-looking value -> stay mcq_single.
    q2 = {"qst_id": 600, "subtype": "mcq_single", "base_slug": "phd",
          "prompt": "What is x?",
          "options": [{"label": l, "text": v, "is_correct": False}
                      for l, v in zip("ABCDE", ["10", "20", "30", "60", "100"])],
          "correct_label": "60", "figure_refs": []}
    assert reclassify_subtype_from_answer_key(q2) == "mcq_single"


def test_reclassify_skips_verbal_subtypes():
    """rcd / tcd / sed must not be reclassified — their subtype rides on
    the chapter slug, not the answer-key shape."""
    q = {"qst_id": 1, "subtype": "tc", "base_slug": "tcd",
         "prompt": "...", "options": [], "correct_label": "60"}
    assert reclassify_subtype_from_answer_key(q) == "tc"


def test_parse_numeric_value_handles_unicode_minus():
    """Princeton answer keys use U+2212 / en-dash / em-dash for negatives."""
    assert parse_numeric_value("\u22121") == -1.0
    assert parse_numeric_value("\u20131") == -1.0
    assert parse_numeric_value("\u20141") == -1.0
    # Unicode minus inside a fraction
    val = parse_numeric_value("\u221280/81")
    assert val is not None
    assert val < -0.987 and val > -0.989


def test_g10_allows_short_prompt_when_stem_is_an_image():
    """Stem-as-image questions (eg qst566) have empty/short prompt text
    but carry the equation in figure_refs. G10 must not fail them."""
    q = {"qst_id": 566, "subtype": "numeric_entry", "prompt": "",
         "figure_refs": [{"filename": "x.gif", "kind": "diagram"}],
         "options": [], "correct_label": "5"}
    v = run_validation_gates(q, {})
    assert "G10_prompt_length_sane" not in v["failed_gates"]


def test_g9_qc_passes_when_quantity_labels_in_stem_image():
    """qst526/534: full QC stem rendered as a single GIF; the 'Quantity A:'
    text isn't in the prompt body — that's not a real defect."""
    q = {"qst_id": 526, "subtype": "qc", "prompt": "",
         "figure_refs": [{"filename": "x.gif", "kind": "diagram"}],
         "options": [
             {"label": "A", "text": "Quantity A is greater.", "is_correct": False},
             {"label": "B", "text": "Quantity B is greater.", "is_correct": True},
             {"label": "C", "text": "The two quantities are equal.", "is_correct": False},
             {"label": "D", "text": "Cannot be determined.", "is_correct": False},
         ], "correct_label": "B"}
    v = run_validation_gates(q, {})
    assert "G9_qc_quantity_labels" not in v["failed_gates"]


def test_subtype_option_counts_allows_mcq_multi_up_to_10():
    """qst890 (x^2+x-20=0 multi-select) has 8 numeric options."""
    assert 8 in SUBTYPE_OPTION_COUNTS["mcq_multi"]
    assert 10 in SUBTYPE_OPTION_COUNTS["mcq_multi"]


def test_quant_subtype_detects_plural_verb_multi():
    """Princeton uses 'Which of the following ARE/SATISFY/HAVE...' in
    place of 'indicate all such' for multi-select."""
    from scripts.extract_princeton import _detect_quant_subtype
    body = ('<root><p>Which of the following values of x satisfy the equation?</p></root>')
    assert _detect_quant_subtype(body, has_options=True, has_numeric_box=False) \
        == "mcq_multi"
    body2 = ('<root><p>Which of the following are factors of 24?</p></root>')
    assert _detect_quant_subtype(body2, has_options=True, has_numeric_box=False) \
        == "mcq_multi"
    body3 = ('<root><p>What is the value of x?</p></root>')
    assert _detect_quant_subtype(body3, has_options=True, has_numeric_box=False) \
        == "mcq_single"


# Regression tests for the stem-image-as-equation defect class -----------


def test_text_from_inserts_inline_gif_placeholder():
    """An inline-sized publisher GIF (operator glyph) inside a stem must
    leave a ``[img:...]`` placeholder in the prompt text so the inline-
    math substitution pass can replace it with rendered text. Without
    this the prompt would be silently empty for stem-as-image questions
    (qst611 / qst524 / qst896 etc.)."""
    soup = BeautifulSoup(
        '<p>If <img class="inline" src="images/Revi_99_epub_546_r1.gif" '
        'width="70" height="24"/></p>',
        "html.parser",
    )
    out = _text_from(soup)
    assert "[img:Revi_99_epub_546_r1.gif]" in out


def test_text_from_drops_large_diagram_image():
    """A larger publisher GIF (a real diagram, the renderer will show it
    visually via figure_refs) must NOT leak as a placeholder in the
    prompt text — that would duplicate the image."""
    soup = BeautifulSoup(
        '<p><img src="images/Revi_99_epub_872_r1.jpg" width="160" '
        'height="120"/> Diagram caption text.</p>',
        "html.parser",
    )
    out = _text_from(soup)
    assert "[img:" not in out
    assert "Diagram caption text" in out


def test_text_from_drops_bullet_and_numeric_box():
    """Bullet glyphs and numeric-entry boxes never become prompt text."""
    bullet = next(iter(BULLET_IMAGE_FILES))
    box = next(iter(NUMERIC_ENTRY_BOX_FILES))
    soup = BeautifulSoup(
        '<p>x = <img src="images/' + bullet + '"/> '
        '<img src="images/' + box + '"/> end</p>',
        "html.parser",
    )
    out = _text_from(soup)
    assert "[img:" not in out
    assert bullet not in out
    assert box not in out
    assert "x =" in out and "end" in out


def test_text_from_inlines_fraction_gif():
    """``frac1-2`` filename collapses to ``(1/2)`` text without a
    placeholder."""
    soup = BeautifulSoup(
        '<p>x = <img class="inline" '
        'src="images/Revi_99_epub_frac1-2_r1.gif"/></p>',
        "html.parser",
    )
    out = _text_from(soup)
    assert "(1/2)" in out
    assert "[img:" not in out


def test_g10_accepts_short_math_prompt():
    """Pure-math prompts like ``sqrt(81 + 9) =`` are valid even though
    they're under 30 chars. G10 must accept them when they look like a
    math expression."""
    q = {"qst_id": 611, "subtype": "mcq_single", "prompt": "sqrt(81 + 9) =",
         "figure_refs": [], "inline_gif_targets": [],
         "options": [{"label": "A", "text": "9", "is_correct": False}],
         "correct_label": "C"}
    v = run_validation_gates(q, {})
    assert "G10_prompt_length_sane" not in v["failed_gates"]


def test_g10_accepts_short_equation_prompt():
    """``If (2x + 2)^{2}=0, then x =`` (qst881) — equation context, short
    by chars, should pass."""
    q = {"qst_id": 881, "subtype": "numeric_entry",
         "prompt": "If (2x + 2)^{2}=0, then x =",
         "figure_refs": [], "inline_gif_targets": [],
         "options": [], "correct_label": "-1",
         "numeric_answer": {"mode": "decimal", "value": -1}}
    v = run_validation_gates(q, {})
    assert "G10_prompt_length_sane" not in v["failed_gates"]


def test_g10_still_rejects_short_prose_prompt():
    """A short non-math prompt (e.g. ``Hello world``) must still fail."""
    q = {"qst_id": 0, "subtype": "mcq_single", "prompt": "Hello world",
         "figure_refs": [], "inline_gif_targets": [],
         "options": [{"label": "A", "text": "x", "is_correct": False}],
         "correct_label": "A"}
    v = run_validation_gates(q, {})
    assert "G10_prompt_length_sane" in v["failed_gates"]


def test_g4_rc_select_passage_accepts_single_letter_label():
    """Princeton's answer key for ``rc_select_passage`` sometimes prints
    a single letter (E) that indexes into the passage's sentences. The
    runtime resolves letter -> sentence via passage tokenisation, so the
    gate must accept a single-letter label."""
    q = {"qst_id": 168, "subtype": "rc_select_passage", "options": [],
         "correct_label": "E", "prompt": "Select the sentence that ...",
         "stimulus_text": "Some passage with several sentences.",
         "figure_refs": [], "inline_gif_targets": []}
    v = run_validation_gates(q, {})
    assert "G4_answer_key_paired" not in v["failed_gates"]


def test_g4_rc_select_passage_accepts_full_sentence_label():
    """Existing behaviour: a literal sentence answer (>=10 chars) still
    passes."""
    q = {"qst_id": 169, "subtype": "rc_select_passage", "options": [],
         "correct_label": "Black smokers piqued the curiosity of biologists.",
         "prompt": "Select the sentence ...",
         "stimulus_text": "Lorem ipsum.",
         "figure_refs": [], "inline_gif_targets": []}
    v = run_validation_gates(q, {})
    assert "G4_answer_key_paired" not in v["failed_gates"]


def test_g4_rc_select_passage_rejects_short_non_letter_label():
    """A 2-3 char non-letter answer (e.g. an OCR fragment) is still
    invalid."""
    q = {"qst_id": 170, "subtype": "rc_select_passage", "options": [],
         "correct_label": "the",
         "prompt": "Select the sentence ...",
         "stimulus_text": "Lorem ipsum.",
         "figure_refs": [], "inline_gif_targets": []}
    v = run_validation_gates(q, {})
    assert "G4_answer_key_paired" in v["failed_gates"]

