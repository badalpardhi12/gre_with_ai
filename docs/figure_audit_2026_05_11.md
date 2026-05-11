# Quant figure-bearing audit — 2026-05-11

Systematic vision audit of every live quant question whose stimulus
ships an embedded image (``data:image/...`` in ``stimulus.content``).
Triggered by a user report that figure↔question mismatches still
occurred after migrations 020/021 retired nine obvious cases.

## Scope

The audit only targeted the bucket where the mismatch failure mode has
ever been observed:

| Bucket | Count | Risk |
|---|---|---|
| ``manhattan_5lb_2018``, embedded image | 36 | **high** — lossy PDF/EPUB extraction duplicated figures across adjacent question pairs |
| ``ai_synthetic``, ``stimulus_type='graph'`` | 6 | low — SVG spec + stem authored together from a single ``render_spec`` |
| ``manhattan_5lb_2018``, HTML ``<table>`` | 5 | low — pure HTML, no image extraction step |
| ``ai_generated``, HTML ``<table>`` | 3 | low — same reason |

AI-generated word-problem items without figures (151 ``passage`` stimuli
for Manhattan QC items) are text-only and not at risk.

## Method

1. Extracted each of the 36 images from ``stimulus.content``, resized to
   600px on the longest edge (to fit under the many-image dimension cap),
   and wrote them alongside a JSON manifest (stem + explanation + options)
   to ``/tmp/figure_audit_<stamp>/``.
2. Dispatched three parallel vision subagents, each auditing 12 items.
   Each was told to Read the image and/or OCR with ``tesseract``, then
   classify as ``MATCH`` / ``MISMATCH`` / ``UNCLEAR`` / ``NO_FIGURE_NEEDED``.
3. Agents were briefed to bias conservative (``UNCLEAR`` acceptable;
   ``MISMATCH`` only on clear evidence) and to cite the specific labels
   or geometry that didn't line up.
4. Every flagged ``MISMATCH`` was re-verified directly (OCR + visual
   inspection on a fresh, cache-busted path) before retiring.

## Result

**1 mismatch confirmed out of 36** → retired in migration 022.

| Qid | Stem says | Figure shows | Action |
|---|---|---|---|
| 3754 | Circle inscribed in square, area = 25π | Tangent from external point B with segment length 8, center O, points A & C on circle | **retired** |

Q3754 is the other half of the same Manhattan duplicate-pair bug that
retired Q3753 in migration 020. The tangent figure was pasted to both
sides of the pair; the stems drift apart so only one stem is served
correctly by the single shared figure. Q3754 went unnoticed in the
first round because its stem also mentions a circle.

The other 35 items verified MATCH. No UNCLEAR flags rose to the level
of actionable uncertainty.

## Future additions

Any new image-bearing quant item landing from a lossy extraction
pipeline (Manhattan 5lb, other PDFs/EPUBs) should be run through the
same audit before flipping to ``status='live'``. The audit workspace
layout + agent prompt above is reusable verbatim; the per-batch cap
of 12 items is what kept agents under the many-image dimension limit.
