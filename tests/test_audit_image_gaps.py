"""Smoke tests for ``scripts/audit_image_gaps.py`` (Phase 5.2).

Goals (per spec):

  * Running the script writes the CSV.
  * The CSV has the documented columns in the documented order.
  * Categorisation matches the documented heuristics for the obvious cases:
      - DI item -> expected_figure_type='chart_or_table'
      - Geometry MCQ -> 'geometric_diagram'
      - Stem with "table above" -> 'table'
      - Stem with "graph above" -> 'chart'
      - Item with a real on-disk figure -> current_state='has_figure'
      - Item with figure_refs but missing file -> 'broken_pointer'
      - Item with no figure_refs -> 'missing_figure_refs'
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


@pytest.fixture
def patched_data_dir(tmp_path, monkeypatch):
    """Point the audit's DATA_DIR at a tmp tree.

    Audits run side-effect-free against the DB but write a CSV under
    ``DATA_DIR/audits/``, so we have to redirect the audit's view of
    DATA_DIR to keep the test hermetic.

    Also evict ``scripts.*`` from ``sys.modules`` so the audit + migrator
    re-bind to the current test's ``temp_db``-provided ORM. (``conftest``
    only evicts ``models`` / ``services`` between tests.)
    """
    import sys
    for mod in [m for m in list(sys.modules)
                if m == "scripts" or m.startswith("scripts.")]:
        del sys.modules[mod]

    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_data)

    # Patch dependent constants captured at import in the migrator
    # (audit reuses the migrator's _resolve_existing).
    from scripts import migrate_figure_refs_to_images_dir as mig
    monkeypatch.setattr(mig, "IMAGES_DIR", tmp_data / "images")
    monkeypatch.setattr(mig, "MIGRATED_SUBDIR",
                        tmp_data / "images" / "figure_refs_migrated")
    monkeypatch.setattr(mig, "EXTRACTED_DIR", tmp_data / "extracted")

    return tmp_data


@pytest.fixture
def populated_db(temp_db, patched_data_dir):
    """Seed a small live pool covering each audit branch."""
    tmp_data = patched_data_dir

    # Pre-create a real figure file so one row resolves.
    real_fig = (tmp_data / "extracted" / "manhattan" / "images"
                / "real_chart.png")
    real_fig.parent.mkdir(parents=True, exist_ok=True)
    real_fig.write_bytes(b"\x89PNG\r\n\x1a\n")

    from models.database import Question

    # 1. DI item with a real figure -> has_figure / chart_or_table.
    # Spec eligibility requires geometry topic OR spatial regex match,
    # so we use a stem mentioning "table above".
    Question.create(
        measure="quant",
        subtype="data_interp",
        prompt="Use the table above to compute revenue.",
        topic="data_interpretation",
        subtopic="bar_chart",
        source="manhattan_5lb_2018",
        status="live",
        figure_refs=json.dumps(["images/real_chart.png"]),
    )

    # 2. Geometry MCQ, no figure_refs -> missing_figure_refs / geometric_diagram.
    Question.create(
        measure="quant",
        subtype="mcq_single",
        prompt="In the figure above, find the area of the triangle.",
        topic="geometry",
        subtopic="triangles",
        source="kaplan_2024",
        status="live",
        figure_refs="[]",
    )

    # 3. Item mentioning "table below" without geometry topic.
    Question.create(
        measure="quant",
        subtype="numeric_entry",
        prompt="Refer to the table below to compute total revenue.",
        topic="arithmetic",
        subtopic="ratios",
        source="ai_generated",
        status="live",
        figure_refs="[]",
    )

    # 4. Item mentioning "graph above" -> chart.
    Question.create(
        measure="quant",
        subtype="qc",
        prompt="The graph above shows the temperature over time.",
        topic="statistics",
        subtopic="time_series",
        source="ai_generated",
        status="live",
        figure_refs="[]",
    )

    # 5. Item that should be IGNORED (subtype not eligible).
    Question.create(
        measure="verbal",
        subtype="tc",
        prompt="The figure above shows a passage.",
        topic="",
        subtopic="",
        source="princeton_2012",
        status="live",
        figure_refs="[]",
    )

    # 6. Geometry item with a broken pointer.
    Question.create(
        measure="quant",
        subtype="mcq_single",
        prompt="In the figure above, x = ?",
        topic="geometry",
        subtopic="circles",
        source="kaplan_2024",
        status="live",
        figure_refs=json.dumps(["images/missing_file.png"]),
    )

    # 7. Item with NO spatial language and no geometry -> NOT eligible.
    Question.create(
        measure="quant",
        subtype="mcq_single",
        prompt="What is 2 + 2?",
        topic="arithmetic",
        subtopic="addition",
        source="ai_generated",
        status="live",
        figure_refs="[]",
    )


def test_csv_is_written_with_expected_columns(populated_db, patched_data_dir):
    from scripts.audit_image_gaps import audit, CSV_HEADER
    out = patched_data_dir / "audits" / "image_gaps_2026_05_18.csv"
    rows_written, summary = audit(out)

    assert out.exists()
    assert rows_written > 0

    with open(out, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)

    assert header == CSV_HEADER
    # 5 eligible rows: DI, geom-MCQ, table-NE, graph-QC, broken-MCQ.
    # tc + plain-arithmetic excluded.
    assert len(rows) == 5


def test_categorisation_matches_heuristics(populated_db, patched_data_dir):
    from scripts.audit_image_gaps import audit
    out = patched_data_dir / "audits" / "image_gaps_2026_05_18.csv"
    audit(out)

    by_prompt = {}
    with open(out, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            by_prompt[row["stem_first120"]] = row

    di_row = next(r for r in by_prompt.values()
                  if r["subtype"] == "data_interp")
    assert di_row["expected_figure_type"] == "chart_or_table"
    assert di_row["current_state"] == "has_figure"
    assert di_row["recommended_action"] == "ok"

    geom_rows = [r for r in by_prompt.values()
                 if r["expected_figure_type"] == "geometric_diagram"]
    assert len(geom_rows) == 2  # one missing, one broken
    states = {r["current_state"] for r in geom_rows}
    assert states == {"missing_figure_refs", "broken_pointer"}

    table_row = next(r for r in by_prompt.values()
                     if r["expected_figure_type"] == "table")
    assert table_row["current_state"] == "missing_figure_refs"
    assert table_row["recommended_action"] in {"synth_geom_figure",
                                                "retire_no_figure_dependency"}

    graph_row = next(r for r in by_prompt.values()
                     if r["expected_figure_type"] == "chart")
    assert graph_row["current_state"] == "missing_figure_refs"


def test_summary_groups_by_source_subtype_state(populated_db, patched_data_dir):
    from scripts.audit_image_gaps import audit
    out = patched_data_dir / "audits" / "image_gaps_2026_05_18.csv"
    _, summary = audit(out)

    # Manhattan DI with real figure -> has_figure entry must be present.
    key = ("manhattan_5lb_2018", "data_interp", "has_figure")
    assert summary[key] == 1

    # Kaplan geometry-circles broken pointer.
    key = ("kaplan_2024", "mcq_single", "broken_pointer")
    assert summary[key] == 1
