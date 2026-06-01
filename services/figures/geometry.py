"""Render GRE geometry figures to PNG/HTML from a stored ``render_spec.spec`` dict.

The seed DB stores geometry items as ``{"kind": "svg_geometry", ..., "spec": {...}}``
but the artwork itself was never shipped for ~22 items. This module re-creates the
figures deterministically with matplotlib (Agg backend, no GUI) so the app can embed
them inline using its existing figure contract::

    <div style="text-align: center; padding: 10px;"><img src="data:image/png;base64,..."></div>

Only the inner ``spec`` dict is consumed here (the one with ``kind``/``params``/
``caption``). Four top-level kinds are supported: triangle, circle, coordinate,
polygon. "Figure not drawn to scale" items only need to be schematically correct
and clearly labeled; exact metric scale is intentionally NOT honored.

Python 3.9 compatible (no ``X | Y`` unions, no ``match`` statements).
"""

import base64
import io
import math
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # headless: must be set before pyplot import

import matplotlib.pyplot as plt  # noqa: E402  (after backend selection)
from matplotlib.patches import Arc, Circle as MplCircle, Polygon as MplPolygon  # noqa: E402

SUPPORTED_KINDS = {"triangle", "circle", "coordinate", "polygon"}

_FIGSIZE = (4.0, 3.0)
_DPI = 110

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_geometry_png_bytes(spec: Dict[str, Any]) -> bytes:
    """Render ``spec`` to raw PNG bytes.

    Raises ``ValueError`` if the top-level ``kind`` is unknown. Unknown
    *sub*-keys degrade gracefully (we draw what we understand and skip the rest).
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dict")
    kind = spec.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise ValueError("Unknown geometry kind: {!r}".format(kind))

    params = spec.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    fig = plt.figure(figsize=_FIGSIZE, dpi=_DPI)
    try:
        ax = fig.add_subplot(111)
        if kind == "triangle":
            _draw_triangle(ax, params)
        elif kind == "circle":
            _draw_circle(ax, params)
        elif kind == "polygon":
            _draw_polygon(ax, params)
        elif kind == "coordinate":
            _draw_coordinate(ax, params)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_geometry_html(spec: Dict[str, Any]) -> str:
    """Render ``spec`` to the app's inline figure HTML snippet.

    Returns a ``<div>`` containing a base64 data-URI PNG and, when present, the
    spec's ``caption`` as a small italic line beneath the image.
    """
    png = render_geometry_png_bytes(spec)
    b64 = base64.b64encode(png).decode("ascii")
    caption = ""
    cap_text = (spec or {}).get("caption")
    if cap_text:
        caption = (
            '<div style="font-size: 11px; font-style: italic; '
            'color: #888; margin-top: 4px;">{}</div>'.format(_escape(str(cap_text)))
        )
    return (
        '<div style="text-align: center; padding: 10px;">'
        '<img src="data:image/png;base64,{b64}">'
        "{caption}"
        "</div>"
    ).format(b64=b64, caption=caption)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LINE = "#222222"
_ACCENT = "#1565c0"
_FILL = "#e8f0fe"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _blank_axes(ax: "plt.Axes") -> None:
    ax.set_aspect("equal")
    ax.axis("off")


def _midpoint(p: Tuple[float, float], q: Tuple[float, float]) -> Tuple[float, float]:
    return ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)


def _outward_label(
    ax: "plt.Axes",
    p: Tuple[float, float],
    q: Tuple[float, float],
    centroid: Tuple[float, float],
    text: str,
    offset: float = 0.08,
) -> None:
    """Place ``text`` near the midpoint of edge p-q, nudged away from ``centroid``."""
    mid = _midpoint(p, q)
    dx, dy = mid[0] - centroid[0], mid[1] - centroid[1]
    norm = math.hypot(dx, dy) or 1.0
    x = mid[0] + offset * dx / norm
    y = mid[1] + offset * dy / norm
    ax.text(x, y, text, ha="center", va="center", fontsize=10, color=_LINE)


# ---------------------------------------------------------------------------
# Triangle
# ---------------------------------------------------------------------------


def _triangle_vertices(params: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    """Return schematic A/B/C positions for the given triangle kind.

    Positions are *schematic only* (figures are "not drawn to scale"); the goal
    is a clearly readable shape with the right structural relationships.
    """
    kind = (params.get("kind") or "scalene").lower()
    right_at = params.get("right_angle_at")
    apex = params.get("apex")

    if kind == "right":
        # Place the right angle vertex at origin with the two legs on the axes.
        verts = {"A": (0.0, 1.4), "B": (0.0, 0.0), "C": (1.7, 0.0)}
        if right_at in ("A", "B", "C"):
            order = {"B": ("A", "B", "C"), "A": ("B", "A", "C"), "C": ("A", "C", "B")}
            top, corner, base = order[right_at]
            verts = {corner: (0.0, 0.0), base: (1.7, 0.0), top: (0.0, 1.4)}
        return verts
    if kind == "equilateral":
        return {"A": (0.0, 1.5), "B": (-0.9, 0.0), "C": (0.9, 0.0)}
    if kind == "isosceles":
        verts = {"A": (0.0, 1.5), "B": (-0.85, 0.0), "C": (0.85, 0.0)}
        if apex in ("A", "B", "C"):
            others = [v for v in ("A", "B", "C") if v != apex]
            verts = {apex: (0.0, 1.5), others[0]: (-0.85, 0.0), others[1]: (0.85, 0.0)}
        return verts
    # scalene / generic: a clearly irregular triangle
    return {"A": (-0.3, 1.5), "B": (-1.0, 0.0), "C": (1.4, 0.0)}


def _draw_triangle(ax: "plt.Axes", params: Dict[str, Any]) -> None:
    verts = _triangle_vertices(params)
    a, b, c = verts["A"], verts["B"], verts["C"]
    centroid = ((a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0)

    poly = MplPolygon([a, b, c], closed=True, fill=True,
                      facecolor=_FILL, edgecolor=_LINE, linewidth=1.8)
    ax.add_patch(poly)

    # Vertex letters, nudged outward from the centroid.
    for name, pt in verts.items():
        dx, dy = pt[0] - centroid[0], pt[1] - centroid[1]
        norm = math.hypot(dx, dy) or 1.0
        ax.text(pt[0] + 0.16 * dx / norm, pt[1] + 0.16 * dy / norm, name,
                ha="center", va="center", fontsize=11, fontweight="bold",
                color=_LINE)

    # Right-angle marker.
    right_at = params.get("right_angle_at")
    if (params.get("kind") or "").lower() == "right" and right_at in verts:
        _draw_right_angle_marker(ax, verts, right_at)

    # Side labels: keyed by 2-letter edge name like "AB"; order-insensitive.
    side_labels = params.get("side_labels") or {}
    if isinstance(side_labels, dict):
        for edge, label in side_labels.items():
            pts = _edge_points(edge, verts)
            if pts and label:
                _outward_label(ax, pts[0], pts[1], centroid, str(label), offset=0.18)

    # Angle labels at vertices.
    angle_labels = params.get("angle_labels") or {}
    if isinstance(angle_labels, dict):
        for vertex, label in angle_labels.items():
            if vertex in verts and label:
                pt = verts[vertex]
                # Pull inward toward centroid so the angle text sits inside.
                dx, dy = centroid[0] - pt[0], centroid[1] - pt[1]
                norm = math.hypot(dx, dy) or 1.0
                ax.text(pt[0] + 0.34 * dx / norm, pt[1] + 0.34 * dy / norm,
                        str(label), ha="center", va="center", fontsize=9,
                        color=_ACCENT)

    _blank_axes(ax)
    ax.relim()
    ax.autoscale_view()
    _pad_limits(ax, 0.25)


def _edge_points(edge: str, verts: Dict[str, Tuple[float, float]]):
    if not isinstance(edge, str) or len(edge) != 2:
        return None
    a, b = edge[0].upper(), edge[1].upper()
    if a in verts and b in verts:
        return verts[a], verts[b]
    return None


def _draw_right_angle_marker(ax, verts, vertex, size=0.18):
    """Draw a small square at ``vertex`` between its two adjacent edges."""
    others = [v for v in ("A", "B", "C") if v != vertex]
    p = verts[vertex]
    dirs = []
    for o in others:
        q = verts[o]
        dx, dy = q[0] - p[0], q[1] - p[1]
        norm = math.hypot(dx, dy) or 1.0
        dirs.append((dx / norm, dy / norm))
    p1 = (p[0] + size * dirs[0][0], p[1] + size * dirs[0][1])
    p3 = (p[0] + size * dirs[1][0], p[1] + size * dirs[1][1])
    p2 = (p1[0] + size * dirs[1][0], p1[1] + size * dirs[1][1])
    ax.add_patch(MplPolygon([p, p1, p2, p3], closed=True, fill=False,
                            edgecolor=_LINE, linewidth=1.2))


# ---------------------------------------------------------------------------
# Circle
# ---------------------------------------------------------------------------


def _draw_circle(ax: "plt.Axes", params: Dict[str, Any]) -> None:
    R = 1.0
    center = (0.0, 0.0)
    ax.add_patch(MplCircle(center, R, fill=False, edgecolor=_LINE, linewidth=1.8))

    # Center dot + optional label.
    center_label = params.get("center_label")
    show_perp = params.get("show_perpendicular")
    if center_label or params.get("show_radius") or show_perp:
        ax.plot([0], [0], marker="o", markersize=3, color=_LINE)
    if center_label:
        ax.text(-0.08, -0.12, str(center_label), ha="right", va="top",
                fontsize=10, fontweight="bold", color=_LINE)

    # Radius line + label.
    radius_label = params.get("radius_label")
    show_radius = params.get("show_radius")
    if show_radius or radius_label is not None:
        # Draw a radius up-right at 60 degrees (clear of any chord at the bottom).
        ang = math.radians(60)
        rx, ry = R * math.cos(ang), R * math.sin(ang)
        ax.plot([0, rx], [0, ry], color=_ACCENT, linewidth=1.4)
        if radius_label is not None:
            ax.text(rx * 0.55 - 0.05, ry * 0.55 + 0.08, str(radius_label),
                    ha="center", va="center", fontsize=9, color=_ACCENT)

    # Chord between two angular positions on the circle.
    chord = params.get("show_chord")
    chord_endpoints = None
    if isinstance(chord, dict):
        a1 = float(chord.get("angle1_deg", 200))
        a2 = float(chord.get("angle2_deg", 340))
        p1 = (R * math.cos(math.radians(a1)), R * math.sin(math.radians(a1)))
        p2 = (R * math.cos(math.radians(a2)), R * math.sin(math.radians(a2)))
        chord_endpoints = (p1, p2)
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=_LINE, linewidth=1.6)
        mid = _midpoint(p1, p2)
        clabel = chord.get("label")
        if clabel:
            # Offset the chord label away from the center.
            dx, dy = mid[0] - center[0], mid[1] - center[1]
            norm = math.hypot(dx, dy) or 1.0
            ax.text(mid[0] + 0.16 * dx / norm, mid[1] + 0.16 * dy / norm,
                    str(clabel), ha="center", va="center", fontsize=9, color=_LINE)
    elif chord is True:
        p1 = (R * math.cos(math.radians(200)), R * math.sin(math.radians(200)))
        p2 = (R * math.cos(math.radians(340)), R * math.sin(math.radians(340)))
        chord_endpoints = (p1, p2)
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=_LINE, linewidth=1.6)

    # Perpendicular from center to the chord (with foot marker + length label).
    perp = params.get("show_perpendicular")
    if perp is not None and perp is not False and chord_endpoints is not None:
        foot = _foot_of_perpendicular(center, chord_endpoints[0], chord_endpoints[1])
        ax.plot([center[0], foot[0]], [center[1], foot[1]],
                color=_ACCENT, linewidth=1.3, linestyle="-")
        _draw_perp_tick(ax, center, foot, chord_endpoints)
        plabel = None
        if isinstance(perp, dict):
            plabel = perp.get("length_label") or perp.get("label")
        if plabel is None:
            plabel = params.get("perpendicular_label")
        if plabel:
            mid = _midpoint(center, foot)
            ax.text(mid[0] + 0.12, mid[1], str(plabel), ha="left", va="center",
                    fontsize=9, color=_ACCENT)

    # Inscribed regular shape (best-effort: triangle or square inscribed).
    inscribed = params.get("inscribed")
    if inscribed:
        n = 3
        if isinstance(inscribed, dict):
            n = int(inscribed.get("n_sides") or inscribed.get("sides") or 3)
        elif isinstance(inscribed, int):
            n = inscribed
        pts = [(R * math.cos(math.radians(90 + 360.0 * k / n)),
                R * math.sin(math.radians(90 + 360.0 * k / n))) for k in range(n)]
        ax.add_patch(MplPolygon(pts, closed=True, fill=False,
                                edgecolor=_ACCENT, linewidth=1.3))

    # Tangent line at the rightmost point (vertical tangent).
    tangent = params.get("show_tangent")
    if tangent:
        ax.plot([R, R], [-0.7, 0.7], color=_ACCENT, linewidth=1.3)
        ax.plot([R], [0], marker="o", markersize=3, color=_LINE)
        tlabel = None
        if isinstance(tangent, dict):
            tlabel = tangent.get("label")
        if tlabel:
            ax.text(R + 0.06, 0.55, str(tlabel), ha="left", va="center",
                    fontsize=9, color=_ACCENT)

    _blank_axes(ax)
    ax.set_xlim(-R - 0.45, R + 0.55)
    ax.set_ylim(-R - 0.35, R + 0.35)


def _foot_of_perpendicular(c, p1, p2):
    """Foot of the perpendicular from point ``c`` onto segment line p1-p2."""
    ax_, ay = p1
    bx, by = p2
    dx, dy = bx - ax_, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return p1
    t = ((c[0] - ax_) * dx + (c[1] - ay) * dy) / denom
    return (ax_ + t * dx, ay + t * dy)


def _draw_perp_tick(ax, c, foot, chord_endpoints, size=0.1):
    """Small right-angle square where the perpendicular meets the chord."""
    p1, p2 = chord_endpoints
    cdx, cdy = (p2[0] - p1[0]), (p2[1] - p1[1])
    cnorm = math.hypot(cdx, cdy) or 1.0
    cdir = (cdx / cnorm, cdy / cnorm)
    pdx, pdy = (c[0] - foot[0]), (c[1] - foot[1])
    pnorm = math.hypot(pdx, pdy) or 1.0
    pdir = (pdx / pnorm, pdy / pnorm)
    a = foot
    b = (a[0] + size * cdir[0], a[1] + size * cdir[1])
    d = (a[0] + size * pdir[0], a[1] + size * pdir[1])
    cc = (b[0] + size * pdir[0], b[1] + size * pdir[1])
    ax.add_patch(MplPolygon([a, b, cc, d], closed=True, fill=False,
                            edgecolor=_ACCENT, linewidth=1.0))


# ---------------------------------------------------------------------------
# Polygon (regular n-gon)
# ---------------------------------------------------------------------------


def _draw_polygon(ax: "plt.Axes", params: Dict[str, Any]) -> None:
    n = int(params.get("n_sides") or params.get("sides") or 3)
    if n < 3:
        n = 3
    R = 1.0
    # Start at the top and go counter-clockwise; rotate so a flat side sits
    # at the bottom for even-sided polygons (looks natural for hexagons etc.).
    start = 90.0 + (180.0 / n)
    pts = [(R * math.cos(math.radians(start + 360.0 * k / n)),
            R * math.sin(math.radians(start + 360.0 * k / n))) for k in range(n)]

    ax.add_patch(MplPolygon(pts, closed=True, fill=True, facecolor=_FILL,
                            edgecolor=_LINE, linewidth=1.8))

    if params.get("show_diagonals"):
        for i in range(n):
            for j in range(i + 1, n):
                # Skip adjacent vertices (those are edges, already drawn).
                if (j - i) % n == 1 or (i - j) % n == 1:
                    continue
                ax.plot([pts[i][0], pts[j][0]], [pts[i][1], pts[j][1]],
                        color=_ACCENT, linewidth=0.7, alpha=0.6)

    side_label = params.get("side_length_label")
    if side_label:
        # Label the bottom-most edge (between the two lowest vertices).
        order = sorted(range(n), key=lambda k: pts[k][1])
        i, j = order[0], order[1]
        mid = _midpoint(pts[i], pts[j])
        ax.text(mid[0], mid[1] - 0.12, str(side_label), ha="center", va="top",
                fontsize=9, color=_LINE)

    interior_label = params.get("interior_angle_label")
    if interior_label:
        # Place near a top vertex, pulled toward the centroid.
        top = max(range(n), key=lambda k: pts[k][1])
        pt = pts[top]
        dx, dy = -pt[0], -pt[1]
        norm = math.hypot(dx, dy) or 1.0
        ax.text(pt[0] + 0.28 * dx / norm, pt[1] + 0.28 * dy / norm,
                str(interior_label), ha="center", va="center", fontsize=9,
                color=_ACCENT)

    _blank_axes(ax)
    ax.set_xlim(-R - 0.3, R + 0.3)
    ax.set_ylim(-R - 0.35, R + 0.3)


# ---------------------------------------------------------------------------
# Coordinate plane
# ---------------------------------------------------------------------------


def _draw_coordinate(ax: "plt.Axes", params: Dict[str, Any]) -> None:
    x_min = float(params.get("x_min", -6))
    x_max = float(params.get("x_max", 6))
    y_min = float(params.get("y_min", -6))
    y_max = float(params.get("y_max", 6))

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.set_aspect("equal", adjustable="box")

    # Axes through the origin (real axes for coordinate kind).
    ax.axhline(0, color="#999999", linewidth=0.9, zorder=1)
    ax.axvline(0, color="#999999", linewidth=0.9, zorder=1)
    ax.grid(True, color="#e0e0e0", linewidth=0.5, zorder=0)
    ax.tick_params(labelsize=7)
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Line: y = slope * x + intercept across the visible x-range.
    line = params.get("line")
    if isinstance(line, dict):
        slope = float(line.get("slope", 0.0))
        intercept = float(line.get("intercept", 0.0))
        xs = [x_min, x_max]
        ys = [slope * x + intercept for x in xs]
        ax.plot(xs, ys, color=_ACCENT, linewidth=1.6, zorder=3)

    # Free-standing segments (e.g. rectangle edges or a single segment).
    segments = params.get("segments")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            frm = seg.get("from")
            to = seg.get("to")
            if (isinstance(frm, (list, tuple)) and isinstance(to, (list, tuple))
                    and len(frm) >= 2 and len(to) >= 2):
                ax.plot([frm[0], to[0]], [frm[1], to[1]], color=_LINE,
                        linewidth=1.5, zorder=2)

    # Plotted points with labels.
    points = params.get("points")
    if isinstance(points, list):
        for pt in points:
            if not isinstance(pt, dict):
                continue
            try:
                px = float(pt.get("x"))
                py = float(pt.get("y"))
            except (TypeError, ValueError):
                continue
            ax.plot([px], [py], marker="o", markersize=5, color="#c62828", zorder=4)
            label = pt.get("label")
            if label:
                ax.annotate(str(label), (px, py), textcoords="offset points",
                            xytext=(6, 6), fontsize=8, color=_LINE)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _pad_limits(ax: "plt.Axes", pad: float) -> None:
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    ax.set_xlim(x0 - pad, x1 + pad)
    ax.set_ylim(y0 - pad, y1 + pad)
