"""
Figure generators for synthetic items.

Two backends:

- `geometry`: programmatic SVG generators for triangle / circle /
  coordinate-plane / polygon / 3D-wireframe stimuli. Output is plain SVG
  text (no rasterization), so the file is small enough to embed in
  markdown and the answer key is provably consistent with the figure.

- `data_interp`: matplotlib-based bar / line / pie / scatter / table
  generators for Data Interpretation clusters. Outputs PNG.

The synthetic plan §6 forbids vision-model image generation: every
figure must be code-built so the drafter's claimed values are the
values rendered. Each generator accepts a `figure_spec` dict produced
by the drafter (or a stand-in built by the seeder) and returns the path
to the saved asset plus a short metadata dict for the question payload.
"""
from services.synthetic.figures.geometry import (
    GeometryFigure, render_geometry,
)
from services.synthetic.figures.data_interp import (
    DataInterpFigure, render_data_interp,
)

__all__ = [
    "GeometryFigure", "render_geometry",
    "DataInterpFigure", "render_data_interp",
]
