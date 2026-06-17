"""Regression tests for quant data-presentation layout routing.

Reported issues:
- A quant MC carrying a DATA TABLE rendered single-column with the table
  clipped in a small scroll box (should be the two-pane split).
- A quant question carrying a DATA CHART image rendered the chart tiny
  (should fill the left stimulus pane).

These lock the `_should_split` / `_is_data_presentation` predicates that route
such questions into the ETS two-pane layout, while keeping QC and small
geometry figures inline.
"""
import pytest

pytest.importorskip("wx", reason="question_screen requires wxPython")

from screens.question_screen import _should_split, _is_data_presentation


def _q(subtype, content=None, stype=None, render_spec=None):
    stim = None
    if content is not None:
        stim = {"content": content, "type": stype, "render_spec": render_spec}
    return {"subtype": subtype, "stimulus": stim}


def test_data_table_splits():
    q = _q("mcq_single", "<table><tr><td>1</td></tr></table>", stype="table")
    assert _is_data_presentation(q) is True
    assert _should_split(q) is True


def test_data_chart_image_splits():
    q = _q("numeric_entry", '<img src="data:image/png;base64,AAAA">', stype="graph")
    assert _is_data_presentation(q) is True
    assert _should_split(q) is True


def test_geometry_figure_stays_inline():
    q = _q("mcq_single", '<div><img src="data:image/png;base64,AAAA"></div>',
           stype="graph", render_spec='{"kind":"svg_geometry","spec":{"kind":"triangle"}}')
    assert _is_data_presentation(q) is False   # geometry, not a data presentation
    assert _should_split(q) is False           # renders inline with the stem


def test_qc_never_splits():
    q = _q("qc", '<img src="x">', stype="graph",
           render_spec='{"kind":"svg_geometry"}')
    assert _should_split(q) is False           # QC uses its own inline 2-column


def test_plain_quant_no_stimulus_inline():
    assert _should_split(_q("mcq_single")) is False
    assert _should_split(_q("numeric_entry")) is False


def test_rc_and_di_always_split():
    assert _should_split(_q("rc_single", "<p>passage</p>", stype="passage")) is True
    assert _should_split(_q("data_interp", "<table><tr><td>1</td></tr></table>",
                            stype="table")) is True
    assert _should_split(_q("rc_select_passage", "<p>p</p>", stype="passage")) is True
