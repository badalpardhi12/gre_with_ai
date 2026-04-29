"""
matplotlib-based generators for Data Interpretation stimuli.

The drafter is asked to emit a `chart_spec` dict shaped like:

    {
      "kind": "bar" | "stacked_bar" | "line" | "pie" | "scatter" | "table",
      "title": "...",
      "x_label": "...",  # optional
      "y_label": "...",  # optional
      "series": [
        {"label": "Region A", "values": [12, 17, 9, 11]},
        {"label": "Region B", "values": [10, 14, 13, 8]},
      ],
      "categories": ["2018", "2019", "2020", "2021"],  # x-axis labels
      "caption": "Source: synthetic data; not drawn to scale.",
    }

`render_data_interp(spec, out_path)` renders the chart to PNG using
matplotlib's `Agg` backend (no display required) and returns a
`DataInterpFigure` metadata struct.

We intentionally use a small, neutral colour palette (no Apple-inspired
greens/oranges, no real-world brand colours) so the figure reads as a
generic prep-bank stimulus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # headless; never opens a window
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.ticker as mticker      # noqa: E402


# Colour-blind safe palette (Wong 2011), order matters for stacked bars.
PALETTE = (
    "#0072B2", "#E69F00", "#009E73", "#D55E00",
    "#CC79A7", "#56B4E9", "#F0E442", "#999999",
)


@dataclass
class DataInterpFigure:
    """Metadata for a rendered DI chart."""
    path: str
    kind: str
    title: str = ""
    caption: str = ""
    width_in: float = 6.0
    height_in: float = 4.0
    spec: Dict[str, Any] = field(default_factory=dict)


def _series_values(series: List[Dict[str, Any]]) -> List[List[float]]:
    out: List[List[float]] = []
    for s in series:
        vals = []
        for v in s.get("values") or []:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                vals.append(0.0)
        out.append(vals)
    return out


def _save(fig, out_path: Path, *, dpi: int = 130) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)


# ── Bar / Stacked Bar ─────────────────────────────────────────────────


def render_bar(spec: Dict[str, Any], out_path: Path,
               *, stacked: bool = False) -> Tuple[float, float]:
    series = spec.get("series") or []
    categories = spec.get("categories") or [
        str(i + 1) for i in range(max(len(s.get("values") or []) for s in series) if series else 1)
    ]
    values = _series_values(series)
    width = 6.4
    height = 4.2
    fig, ax = plt.subplots(figsize=(width, height))
    n_categories = len(categories)
    n_series = len(values)
    if stacked:
        bottom = [0.0] * n_categories
        for i, vals in enumerate(values):
            padded = (vals + [0.0] * n_categories)[:n_categories]
            ax.bar(categories, padded, bottom=bottom,
                   color=PALETTE[i % len(PALETTE)],
                   label=series[i].get("label", f"Series {i+1}"),
                   edgecolor="white", linewidth=0.5)
            bottom = [b + v for b, v in zip(bottom, padded)]
    else:
        bar_w = 0.8 / max(1, n_series)
        for i, vals in enumerate(values):
            padded = (vals + [0.0] * n_categories)[:n_categories]
            xs = [j + (i - (n_series - 1) / 2) * bar_w for j in range(n_categories)]
            ax.bar(xs, padded, width=bar_w,
                   color=PALETTE[i % len(PALETTE)],
                   label=series[i].get("label", f"Series {i+1}"),
                   edgecolor="white", linewidth=0.5)
        ax.set_xticks(range(n_categories))
        ax.set_xticklabels(categories)
    ax.set_title(spec.get("title", ""))
    ax.set_xlabel(spec.get("x_label", ""))
    ax.set_ylabel(spec.get("y_label", ""))
    if n_series > 1:
        ax.legend(loc="best", frameon=False, fontsize=9)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=False, prune=None))
    ax.grid(axis="y", linestyle=":", linewidth=0.6, color="#9ca3af")
    ax.set_axisbelow(True)
    _save(fig, out_path)
    return width, height


# ── Line ──────────────────────────────────────────────────────────────


def render_line(spec: Dict[str, Any], out_path: Path) -> Tuple[float, float]:
    series = spec.get("series") or []
    categories = spec.get("categories") or [
        str(i + 1) for i in range(max(len(s.get("values") or []) for s in series) if series else 1)
    ]
    values = _series_values(series)
    width, height = 6.4, 4.2
    fig, ax = plt.subplots(figsize=(width, height))
    for i, vals in enumerate(values):
        padded = (vals + [None] * len(categories))[:len(categories)]
        ax.plot(categories, padded, marker="o", linewidth=2.0,
                markersize=5,
                color=PALETTE[i % len(PALETTE)],
                label=series[i].get("label", f"Series {i+1}"))
    ax.set_title(spec.get("title", ""))
    ax.set_xlabel(spec.get("x_label", ""))
    ax.set_ylabel(spec.get("y_label", ""))
    if len(values) > 1:
        ax.legend(loc="best", frameon=False, fontsize=9)
    ax.grid(True, linestyle=":", linewidth=0.6, color="#9ca3af")
    ax.set_axisbelow(True)
    _save(fig, out_path)
    return width, height


# ── Pie ───────────────────────────────────────────────────────────────


def render_pie(spec: Dict[str, Any], out_path: Path) -> Tuple[float, float]:
    series = spec.get("series") or []
    if not series:
        labels = []
        sizes: List[float] = []
    else:
        first = series[0]
        labels = first.get("labels") or first.get("categories") or spec.get("categories", [])
        vals = first.get("values") or []
        try:
            sizes = [float(v) for v in vals]
        except (TypeError, ValueError):
            sizes = []
    if not labels and sizes:
        labels = [f"Slice {i+1}" for i in range(len(sizes))]
    width, height = 5.6, 4.6
    fig, ax = plt.subplots(figsize=(width, height))
    if sizes:
        ax.pie(
            sizes,
            labels=labels,
            autopct="%1.1f%%",
            colors=PALETTE[: len(sizes)],
            startangle=90,
            wedgeprops={"linewidth": 1.0, "edgecolor": "white"},
            textprops={"fontsize": 10},
        )
    ax.set_title(spec.get("title", ""))
    ax.axis("equal")
    _save(fig, out_path)
    return width, height


# ── Scatter ───────────────────────────────────────────────────────────


def render_scatter(spec: Dict[str, Any], out_path: Path) -> Tuple[float, float]:
    series = spec.get("series") or []
    width, height = 6.0, 4.4
    fig, ax = plt.subplots(figsize=(width, height))
    for i, s in enumerate(series):
        xs, ys = [], []
        for pt in s.get("points") or []:
            try:
                xs.append(float(pt.get("x")))
                ys.append(float(pt.get("y")))
            except (TypeError, ValueError):
                continue
        ax.scatter(xs, ys, s=42, color=PALETTE[i % len(PALETTE)],
                   label=s.get("label", f"Series {i+1}"),
                   edgecolor="white", linewidth=0.6)
    ax.set_title(spec.get("title", ""))
    ax.set_xlabel(spec.get("x_label", ""))
    ax.set_ylabel(spec.get("y_label", ""))
    ax.grid(True, linestyle=":", linewidth=0.6, color="#9ca3af")
    ax.set_axisbelow(True)
    if len(series) > 1:
        ax.legend(loc="best", frameon=False, fontsize=9)
    _save(fig, out_path)
    return width, height


# ── Table ─────────────────────────────────────────────────────────────


def render_table(spec: Dict[str, Any], out_path: Path) -> Tuple[float, float]:
    """Render a multi-row data table as a PNG via matplotlib.

    spec:
      "columns": list of column labels
      "rows": list of row tuples (cells must be strings or numbers)
      "title": optional caption shown above the table
    """
    columns = list(spec.get("columns") or [])
    rows = [list(r) for r in (spec.get("rows") or [])]
    n_rows = max(1, len(rows))
    n_cols = max(1, len(columns))
    cell_w = 1.1
    cell_h = 0.45
    width = max(4.5, n_cols * cell_w + 1.0)
    height = max(2.5, (n_rows + 2) * cell_h + 1.0)
    fig, ax = plt.subplots(figsize=(width, height))
    ax.axis("off")
    if spec.get("title"):
        ax.set_title(spec["title"], fontsize=12, pad=12)
    table = ax.table(
        cellText=[[str(c) for c in r] for r in rows] or [[""] * n_cols],
        colLabels=columns or None,
        loc="center",
        cellLoc="center",
        colColours=["#e5e7eb"] * n_cols if columns else None,
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.0, 1.4)
    _save(fig, out_path, dpi=140)
    return width, height


# ── Dispatcher ────────────────────────────────────────────────────────


def render_data_interp(chart_spec: Dict[str, Any],
                       out_path: Path) -> DataInterpFigure:
    """Render a DI chart spec to PNG and return metadata.

    Falls back to a bar chart if `kind` is unknown so the pipeline is
    never blocked by a typo in the drafter spec.
    """
    kind = (chart_spec or {}).get("kind", "bar")
    out_path = Path(out_path)
    if kind == "stacked_bar":
        w, h = render_bar(chart_spec, out_path, stacked=True)
    elif kind == "bar":
        w, h = render_bar(chart_spec, out_path)
    elif kind == "line":
        w, h = render_line(chart_spec, out_path)
    elif kind == "pie":
        w, h = render_pie(chart_spec, out_path)
    elif kind == "scatter":
        w, h = render_scatter(chart_spec, out_path)
    elif kind == "table":
        w, h = render_table(chart_spec, out_path)
    else:
        w, h = render_bar(chart_spec, out_path)
    return DataInterpFigure(
        path=str(out_path),
        kind=kind,
        title=chart_spec.get("title", ""),
        caption=chart_spec.get("caption", ""),
        width_in=w,
        height_in=h,
        spec=chart_spec,
    )
