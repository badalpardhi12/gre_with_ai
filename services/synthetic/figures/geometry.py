"""
Programmatic SVG generators for geometry stimuli.

Each public function returns an SVG string (no file I/O); the dispatcher
`render_geometry(figure_spec, out_path)` writes it to disk and returns a
small `GeometryFigure` metadata struct for the persist stage.

The drafter is asked to emit a `figure_spec` dict shaped like:

    {
      "kind": "triangle" | "circle" | "coordinate" | "polygon" | "wireframe",
      "params": {...kind-specific...},
      "labels": {...optional vertex/segment labels...},
      "caption": "Figure not drawn to scale.",
    }

If the drafter omits `figure_spec` we synthesise a minimal one from the
seed's `subtopic` so the pipeline never crashes for lack of a figure.

Quality bar: figures must be readable on a phone screen. We render at
360x360 (approx 4:3) so the SVG embeds cleanly into the markdown
review file without a viewer's aspect-ratio fight.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CANVAS_W = 360
CANVAS_H = 360
PAD = 30
STROKE = "#1f2937"          # slate-800
ACCENT = "#2563eb"          # blue-600
GRID = "#e5e7eb"            # gray-200
LABEL = "#111827"           # gray-900


@dataclass
class GeometryFigure:
    """Metadata for a rendered geometry SVG."""
    path: str
    kind: str
    width: int = CANVAS_W
    height: int = CANVAS_H
    caption: str = ""
    spec: Dict[str, Any] = field(default_factory=dict)


# ── Primitives ────────────────────────────────────────────────────────


def _svg_open(width: int = CANVAS_W, height: int = CANVAS_H) -> List[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="Helvetica, Arial, sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="white"/>',
    ]


def _svg_close() -> str:
    return "</svg>"


def _line(x1: float, y1: float, x2: float, y2: float,
          *, color: str = STROKE, width: float = 1.6) -> str:
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>'
    )


def _polyline(points: List[Tuple[float, float]],
              *, color: str = STROKE, width: float = 1.6,
              fill: str = "none", closed: bool = True) -> str:
    pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    tag = "polygon" if closed else "polyline"
    return (
        f'<{tag} points="{pts}" stroke="{color}" stroke-width="{width}" '
        f'fill="{fill}" stroke-linejoin="round"/>'
    )


def _text(x: float, y: float, content: str,
          *, anchor: str = "middle", color: str = LABEL,
          size: int = 13) -> str:
    safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" '
        f'font-size="{size}" fill="{color}">{safe}</text>'
    )


def _circle(cx: float, cy: float, r: float, *,
            color: str = STROKE, width: float = 1.6,
            fill: str = "none") -> str:
    return (
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
        f'stroke="{color}" stroke-width="{width}" fill="{fill}"/>'
    )


def _arc(cx: float, cy: float, r: float,
         start_deg: float, end_deg: float, *,
         color: str = ACCENT, width: float = 2.0) -> str:
    # SVG arc path. Uses the large-arc flag and sweep flag.
    start_rad = math.radians(start_deg)
    end_rad = math.radians(end_deg)
    x1, y1 = cx + r * math.cos(start_rad), cy - r * math.sin(start_rad)
    x2, y2 = cx + r * math.cos(end_rad), cy - r * math.sin(end_rad)
    sweep = (end_deg - start_deg) % 360
    large = 1 if sweep > 180 else 0
    return (
        f'<path d="M {x1:.2f} {y1:.2f} A {r:.2f} {r:.2f} 0 {large} 0 '
        f'{x2:.2f} {y2:.2f}" stroke="{color}" stroke-width="{width}" '
        f'fill="none"/>'
    )


def _right_angle_marker(corner: Tuple[float, float],
                        leg_a: Tuple[float, float],
                        leg_b: Tuple[float, float],
                        *, size: float = 12) -> str:
    """Tiny square at a right angle. corner is the vertex; the two leg
    points define the directions of the two legs."""
    cx, cy = corner
    ax, ay = leg_a
    bx, by = leg_b
    # Unit vectors along each leg.
    da_x, da_y = ax - cx, ay - cy
    db_x, db_y = bx - cx, by - cy
    la = math.hypot(da_x, da_y) or 1
    lb = math.hypot(db_x, db_y) or 1
    ua_x, ua_y = da_x / la, da_y / la
    ub_x, ub_y = db_x / lb, db_y / lb
    p1 = (cx + ua_x * size, cy + ua_y * size)
    p2 = (cx + ua_x * size + ub_x * size, cy + ua_y * size + ub_y * size)
    p3 = (cx + ub_x * size, cy + ub_y * size)
    return _polyline([(cx, cy), p1, p2, p3], width=1.0, closed=True,
                     fill="none")


# ── Triangle ──────────────────────────────────────────────────────────


def render_triangle(params: Dict[str, Any],
                    labels: Optional[Dict[str, str]] = None) -> str:
    """Render a triangle in the canvas.

    params:
      - "kind": "right" | "equilateral" | "isosceles" | "scalene"
                (default "scalene")
      - "vertices": optional explicit (x, y) trio in [0,1] coords. If
                     omitted, we synthesise a sensible layout per `kind`.
      - "side_labels": optional dict of side -> label, e.g. {"AB": "5"}
      - "angle_labels": optional dict of vertex -> "x°"
      - "right_angle_at": vertex name where to draw the right-angle box

    Vertex labels default to A, B, C unless overridden in `labels`.
    """
    labels = dict(labels or {})
    kind = params.get("kind", "scalene")

    vert_norm = params.get("vertices")
    if not vert_norm:
        if kind == "right":
            vert_norm = [(0.15, 0.85), (0.85, 0.85), (0.15, 0.15)]
        elif kind == "equilateral":
            vert_norm = [(0.50, 0.10), (0.10, 0.85), (0.90, 0.85)]
        elif kind == "isosceles":
            vert_norm = [(0.50, 0.12), (0.18, 0.85), (0.82, 0.85)]
        else:
            vert_norm = [(0.20, 0.85), (0.80, 0.78), (0.55, 0.18)]

    def _to_canvas(p: Tuple[float, float]) -> Tuple[float, float]:
        return (PAD + p[0] * (CANVAS_W - 2 * PAD),
                PAD + p[1] * (CANVAS_H - 2 * PAD))

    points = [_to_canvas(v) for v in vert_norm[:3]]
    out = _svg_open()
    out.append(_polyline(points, closed=True, fill="#f3f4f6"))

    vertex_labels = ["A", "B", "C"]
    label_offsets = [(0, -10), (-12, 12), (12, 12)]
    for i, (pt, lbl, off) in enumerate(zip(points, vertex_labels, label_offsets)):
        name = labels.get(f"vertex_{lbl}", lbl)
        out.append(_text(pt[0] + off[0], pt[1] + off[1], name, size=14))

    side_labels = params.get("side_labels", {})
    for sd, lbl in side_labels.items():
        # sd like "AB" -> midpoint of A and B
        if len(sd) != 2 or sd[0] not in "ABC" or sd[1] not in "ABC":
            continue
        i, j = vertex_labels.index(sd[0]), vertex_labels.index(sd[1])
        mx = (points[i][0] + points[j][0]) / 2
        my = (points[i][1] + points[j][1]) / 2
        # Push label outward, away from the centroid.
        cx = sum(p[0] for p in points) / 3
        cy = sum(p[1] for p in points) / 3
        dx, dy = mx - cx, my - cy
        d = math.hypot(dx, dy) or 1
        ox, oy = mx + 14 * dx / d, my + 14 * dy / d
        out.append(_text(ox, oy, str(lbl), size=12, color=ACCENT))

    angle_labels = params.get("angle_labels", {})
    for vlbl, txt in angle_labels.items():
        if vlbl not in vertex_labels:
            continue
        i = vertex_labels.index(vlbl)
        cx = sum(p[0] for p in points) / 3
        cy = sum(p[1] for p in points) / 3
        # pull angle label *into* the triangle.
        px, py = points[i]
        dx, dy = cx - px, cy - py
        d = math.hypot(dx, dy) or 1
        ax, ay = px + 22 * dx / d, py + 22 * dy / d
        out.append(_text(ax, ay, str(txt), size=11, color=ACCENT))

    if params.get("right_angle_at") in vertex_labels:
        i = vertex_labels.index(params["right_angle_at"])
        others = [points[j] for j in range(3) if j != i]
        out.append(_right_angle_marker(points[i], others[0], others[1]))

    out.append(_svg_close())
    return "\n".join(out)


# ── Circle ────────────────────────────────────────────────────────────


def render_circle(params: Dict[str, Any],
                  labels: Optional[Dict[str, str]] = None) -> str:
    """Render a circle with optional radius/diameter/chord/sector.

    params:
      - "radius_label": e.g. "r" or "5"
      - "show_diameter": bool
      - "show_chord": optional dict {"angle1_deg": x, "angle2_deg": y,
                                     "label": "..."}
      - "show_sector": optional dict {"start_deg": ..., "end_deg": ...,
                                      "label": "60°"}
      - "show_center": bool (default True)
    """
    labels = dict(labels or {})
    cx, cy = CANVAS_W / 2, CANVAS_H / 2
    r = min(CANVAS_W, CANVAS_H) / 2 - PAD
    out = _svg_open()
    out.append(_circle(cx, cy, r, fill="#f3f4f6"))

    show_center = params.get("show_center", True)
    if show_center:
        out.append(_circle(cx, cy, 2.0, color=STROKE, fill=STROKE))
        out.append(_text(cx - 14, cy - 6,
                         labels.get("center", "O"), size=13))

    radius_label = params.get("radius_label")
    if radius_label:
        # Draw radius to the 30° point (upper-right) so it doesn't fight
        # any chord/sector at 0/90/180/270.
        angle = math.radians(30)
        rx, ry = cx + r * math.cos(angle), cy - r * math.sin(angle)
        out.append(_line(cx, cy, rx, ry, color=ACCENT, width=2.0))
        mx, my = (cx + rx) / 2, (cy + ry) / 2
        out.append(_text(mx + 6, my - 8, str(radius_label),
                         size=12, color=ACCENT))

    if params.get("show_diameter"):
        out.append(_line(cx - r, cy, cx + r, cy, color=ACCENT, width=2.0))

    chord = params.get("show_chord")
    if chord:
        a1 = math.radians(float(chord.get("angle1_deg", 30)))
        a2 = math.radians(float(chord.get("angle2_deg", 150)))
        x1, y1 = cx + r * math.cos(a1), cy - r * math.sin(a1)
        x2, y2 = cx + r * math.cos(a2), cy - r * math.sin(a2)
        out.append(_line(x1, y1, x2, y2, color=ACCENT, width=2.0))
        if chord.get("label"):
            out.append(_text((x1 + x2) / 2, (y1 + y2) / 2 - 8,
                             str(chord["label"]), size=12, color=ACCENT))

    sector = params.get("show_sector")
    if sector:
        s = float(sector.get("start_deg", 0))
        e = float(sector.get("end_deg", 60))
        # Two radii bounding the sector
        for ang in (s, e):
            arad = math.radians(ang)
            ex, ey = cx + r * math.cos(arad), cy - r * math.sin(arad)
            out.append(_line(cx, cy, ex, ey, color="#9333ea", width=2.0))
        out.append(_arc(cx, cy, r * 0.35, s, e, color="#9333ea"))
        if sector.get("label"):
            mid = math.radians((s + e) / 2)
            lx, ly = cx + r * 0.55 * math.cos(mid), cy - r * 0.55 * math.sin(mid)
            out.append(_text(lx, ly, str(sector["label"]), size=12,
                             color="#9333ea"))

    out.append(_svg_close())
    return "\n".join(out)


# ── Coordinate plane ──────────────────────────────────────────────────


def render_coordinate(params: Dict[str, Any],
                      labels: Optional[Dict[str, str]] = None) -> str:
    """Render an x-y plane with grid + optional points / line / parabola.

    params:
      - "x_min", "x_max", "y_min", "y_max" (default -5..5)
      - "points": list of {"x": .., "y": .., "label": ".."} dicts
      - "line": {"slope": m, "intercept": b}  -> y = mx + b
      - "parabola": {"a": .., "h": .., "k": ..}  -> y = a(x-h)^2 + k
      - "show_grid": bool (default True)
    """
    labels = dict(labels or {})
    x_min = float(params.get("x_min", -5))
    x_max = float(params.get("x_max", 5))
    y_min = float(params.get("y_min", -5))
    y_max = float(params.get("y_max", 5))
    x_max = max(x_max, x_min + 1)
    y_max = max(y_max, y_min + 1)

    inner_w = CANVAS_W - 2 * PAD
    inner_h = CANVAS_H - 2 * PAD

    def to_canvas(x: float, y: float) -> Tuple[float, float]:
        cx = PAD + (x - x_min) / (x_max - x_min) * inner_w
        cy = PAD + (y_max - y) / (y_max - y_min) * inner_h
        return cx, cy

    out = _svg_open()
    if params.get("show_grid", True):
        # Vertical grid lines
        x_int = max(1, int(round((x_max - x_min) / 10))) or 1
        x_val = math.ceil(x_min)
        while x_val <= x_max:
            cx, _ = to_canvas(x_val, 0)
            out.append(_line(cx, PAD, cx, CANVAS_H - PAD, color=GRID, width=0.8))
            x_val += x_int
        y_int = max(1, int(round((y_max - y_min) / 10))) or 1
        y_val = math.ceil(y_min)
        while y_val <= y_max:
            _, cy = to_canvas(0, y_val)
            out.append(_line(PAD, cy, CANVAS_W - PAD, cy, color=GRID, width=0.8))
            y_val += y_int

    # Axes
    if x_min <= 0 <= x_max:
        cx0, _ = to_canvas(0, 0)
        out.append(_line(cx0, PAD, cx0, CANVAS_H - PAD, color=STROKE, width=1.4))
    if y_min <= 0 <= y_max:
        _, cy0 = to_canvas(0, 0)
        out.append(_line(PAD, cy0, CANVAS_W - PAD, cy0, color=STROKE, width=1.4))

    # Axis labels: x at (x_max, 0), y at (0, y_max)
    if x_min <= 0 <= x_max and y_min <= 0 <= y_max:
        cx0, cy0 = to_canvas(0, 0)
        out.append(_text(CANVAS_W - PAD + 8, cy0 + 4, "x", size=13))
        out.append(_text(cx0 - 4, PAD - 8, "y", size=13, anchor="end"))
        # Origin marker
        out.append(_text(cx0 - 8, cy0 + 14, "O", size=11))

    line = params.get("line")
    if isinstance(line, dict):
        m = float(line.get("slope", 0))
        b = float(line.get("intercept", 0))
        x0, y0 = x_min, m * x_min + b
        x1, y1 = x_max, m * x_max + b
        c0 = to_canvas(x0, y0)
        c1 = to_canvas(x1, y1)
        out.append(_line(c0[0], c0[1], c1[0], c1[1], color=ACCENT, width=2.0))

    parabola = params.get("parabola")
    if isinstance(parabola, dict):
        a = float(parabola.get("a", 1))
        h = float(parabola.get("h", 0))
        k = float(parabola.get("k", 0))
        # 60-point sampling for a smooth curve.
        pts = []
        for i in range(61):
            x = x_min + i * (x_max - x_min) / 60
            y = a * (x - h) ** 2 + k
            if y_min - 2 <= y <= y_max + 2:
                pts.append(to_canvas(x, y))
        if pts:
            out.append(_polyline(pts, closed=False, color=ACCENT, width=2.0))

    points = params.get("points") or []
    for pdef in points:
        try:
            px = float(pdef.get("x"))
            py = float(pdef.get("y"))
        except (TypeError, ValueError):
            continue
        cx, cy = to_canvas(px, py)
        out.append(_circle(cx, cy, 4.0, color=ACCENT, fill=ACCENT))
        lbl = pdef.get("label", "")
        if lbl:
            out.append(_text(cx + 6, cy - 6, str(lbl), size=12, color=LABEL,
                             anchor="start"))
    out.append(_svg_close())
    return "\n".join(out)


# ── Polygon ───────────────────────────────────────────────────────────


def render_polygon(params: Dict[str, Any],
                   labels: Optional[Dict[str, str]] = None) -> str:
    """Render a regular or arbitrary polygon.

    params:
      - "n_sides": int (>=3)
      - "regular": bool (default True; if False, uses provided vertices)
      - "vertices": list of (x, y) in [0,1] when regular=False
      - "side_labels": {edge_index: "label"}
      - "interior_angle_label": optional string at center
    """
    labels = dict(labels or {})
    out = _svg_open()
    n = int(params.get("n_sides", 5))
    n = max(3, n)
    cx, cy = CANVAS_W / 2, CANVAS_H / 2
    r = min(CANVAS_W, CANVAS_H) / 2 - PAD - 10

    if params.get("regular", True):
        pts = []
        for i in range(n):
            theta = -math.pi / 2 + 2 * math.pi * i / n
            pts.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    else:
        verts = params.get("vertices", [])
        pts = [
            (PAD + v[0] * (CANVAS_W - 2 * PAD),
             PAD + v[1] * (CANVAS_H - 2 * PAD))
            for v in verts
        ]
        if len(pts) < 3:
            pts = [(cx + r * math.cos(-math.pi / 2 + 2 * math.pi * i / n),
                    cy + r * math.sin(-math.pi / 2 + 2 * math.pi * i / n))
                   for i in range(n)]

    out.append(_polyline(pts, closed=True, fill="#f3f4f6"))

    side_labels = params.get("side_labels", {})
    for i, lbl in side_labels.items():
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= len(pts):
            continue
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        # Push outward
        dx, dy = mx - cx, my - cy
        d = math.hypot(dx, dy) or 1
        ox, oy = mx + 14 * dx / d, my + 14 * dy / d
        out.append(_text(ox, oy, str(lbl), size=12, color=ACCENT))

    if params.get("interior_angle_label"):
        out.append(_text(cx, cy + 4, str(params["interior_angle_label"]),
                         size=12, color="#7c3aed"))

    out.append(_svg_close())
    return "\n".join(out)


# ── 3D wireframe (rectangular solid / cylinder hint) ─────────────────


def render_wireframe(params: Dict[str, Any],
                     labels: Optional[Dict[str, str]] = None) -> str:
    """Render a 3D wireframe rectangular solid (the GRE solids workhorse).

    params:
      - "shape": "box" (default) | "cube" | "cylinder"
      - "edge_labels": {"width": "5", "depth": "3", "height": "4"}
    """
    labels = dict(labels or {})
    shape = params.get("shape", "box")
    out = _svg_open()
    if shape in ("box", "cube"):
        # Front face
        x0, y0 = 80, 230
        w, h = 170, 110
        # Back face offset
        ox, oy = 50, -50
        # Front rectangle
        out.append(_polyline(
            [(x0, y0), (x0 + w, y0), (x0 + w, y0 - h), (x0, y0 - h)],
            closed=True, fill="#f3f4f6",
        ))
        # Back rectangle (dashed via lighter color since SVG dashed needs
        # extra attribute)
        out.append(
            f'<polygon points="'
            f'{x0+ox:.2f},{y0+oy:.2f} {x0+w+ox:.2f},{y0+oy:.2f} '
            f'{x0+w+ox:.2f},{y0-h+oy:.2f} {x0+ox:.2f},{y0-h+oy:.2f}" '
            f'stroke="{STROKE}" stroke-width="1.2" stroke-dasharray="4 3" '
            f'fill="none"/>'
        )
        # Connecting edges
        for (px, py) in [(x0, y0), (x0 + w, y0),
                          (x0 + w, y0 - h), (x0, y0 - h)]:
            out.append(_line(px, py, px + ox, py + oy, width=1.2))
        # Edge labels
        edge_labels = params.get("edge_labels", {})
        if "width" in edge_labels:
            out.append(_text(x0 + w / 2, y0 + 16,
                             str(edge_labels["width"]), size=12, color=ACCENT))
        if "height" in edge_labels:
            out.append(_text(x0 - 14, y0 - h / 2,
                             str(edge_labels["height"]), size=12,
                             color=ACCENT, anchor="end"))
        if "depth" in edge_labels:
            mid_x = x0 + w + ox / 2
            mid_y = y0 + oy / 2
            out.append(_text(mid_x + 6, mid_y + 4,
                             str(edge_labels["depth"]), size=12,
                             color=ACCENT, anchor="start"))
    else:  # cylinder hint
        cx, cy = CANVAS_W / 2, CANVAS_H / 2
        rx, ry = 70, 18
        h = 130
        # Top + bottom ellipses
        out.append(
            f'<ellipse cx="{cx:.2f}" cy="{cy-h/2:.2f}" rx="{rx}" ry="{ry}" '
            f'stroke="{STROKE}" stroke-width="1.4" fill="#f3f4f6"/>'
        )
        out.append(
            f'<ellipse cx="{cx:.2f}" cy="{cy+h/2:.2f}" rx="{rx}" ry="{ry}" '
            f'stroke="{STROKE}" stroke-width="1.4" fill="none"/>'
        )
        out.append(_line(cx - rx, cy - h / 2, cx - rx, cy + h / 2, width=1.4))
        out.append(_line(cx + rx, cy - h / 2, cx + rx, cy + h / 2, width=1.4))
        edge_labels = params.get("edge_labels", {})
        if "radius" in edge_labels:
            out.append(_line(cx, cy - h / 2, cx + rx, cy - h / 2,
                             color=ACCENT, width=1.6))
            out.append(_text(cx + rx / 2, cy - h / 2 - 6,
                             str(edge_labels["radius"]),
                             color=ACCENT, size=12))
        if "height" in edge_labels:
            out.append(_text(cx + rx + 14, cy,
                             str(edge_labels["height"]),
                             color=ACCENT, size=12, anchor="start"))

    out.append(_svg_close())
    return "\n".join(out)


# ── Dispatcher ────────────────────────────────────────────────────────


_RENDERERS = {
    "triangle": render_triangle,
    "circle": render_circle,
    "coordinate": render_coordinate,
    "coordinate_plane": render_coordinate,
    "polygon": render_polygon,
    "quadrilateral": render_polygon,
    "wireframe": render_wireframe,
    "solid_3d": render_wireframe,
    "box": render_wireframe,
    "cylinder": render_wireframe,
}


def render_geometry(figure_spec: Dict[str, Any],
                    out_path: Path) -> GeometryFigure:
    """Render a geometry SVG to disk and return metadata.

    `figure_spec` shape:
      {"kind": "triangle", "params": {...}, "labels": {...},
       "caption": "Figure not drawn to scale."}

    If `kind` is unknown, falls back to a generic triangle so the
    pipeline never crashes for a typo in a drafter spec.
    """
    kind = (figure_spec or {}).get("kind", "triangle")
    if kind == "cylinder":
        params = dict(figure_spec.get("params") or {})
        params.setdefault("shape", "cylinder")
    elif kind == "box" or kind == "solid_3d":
        params = dict(figure_spec.get("params") or {})
        params.setdefault("shape", "box")
    else:
        params = dict(figure_spec.get("params") or {})
    renderer = _RENDERERS.get(kind, render_triangle)
    svg = renderer(params, figure_spec.get("labels"))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")
    return GeometryFigure(
        path=str(out_path),
        kind=kind,
        caption=figure_spec.get("caption", "Figure not drawn to scale."),
        spec=figure_spec,
    )
