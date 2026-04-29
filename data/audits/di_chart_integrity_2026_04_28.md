# DI / chart-dependent question integrity audit — 2026-04-28

## Symptom
User sees Quant Section 1 Q1 (golf-equipment production) with an empty Passage
pane: the question references chart data that isn't rendered.

## Root cause
**Data-linkage regression, not a UI regression.**

The wxPython `MathView` widget in `widgets/math_view.py` uses
`wx.html2.WebView.SetPage`, and `widgets/html_sanitizer.py` allow-lists `<img>`
plus the `data:` protocol. Stimuli that carry base64-encoded chart PNGs (or
inline HTML tables) render correctly — e.g., stimulus id 467 (the real golf
equipment chart, 46 606 bytes of base64 PNG) works fine when a question points
to it.

The regression is that live questions point to **empty shell stimulus rows**
(`length(content)=0`, `length(render_spec)=0`) while the populated originals
sit on retired-question rows. Specifically, every live golf-equipment question
in the user's mock points to stim 1020 (empty), but the actual charts live
on stim 467 (PNG) and stim 468 (HTML table).

## Scope

### Live-pool audit (status='live')

| Category | Count |
|---|---|
| Live questions total | 2 990 |
| Live on empty-content chart stimulus | 51 |
| Live with `stimulus_id IS NULL` but prompt cites a figure / graph / chart | 48 |
| **Total chart-integrity defects** | **99 (3.3 % of live pool)** |

### Empty-content chart stimuli

25 rows in `stimulus` with `stimulus_type IN ('graph','table')` and
`length(content) + length(render_spec) < 20`, all with titles of the form
`princeton_cgdN_pM_qX-Y` — shells emitted by the Princeton CGD extractor
that never had the chart image inlined.

### Recoverability via retired-twin remap

The original charts survive on retired question rows (duplicates created
during a consolidation merge). For each live question on an empty stim, we
look for a retired question with the identical `prompt` whose stimulus has
content (populated).

| Remap class | Count | Action |
|---|---|---|
| Direct prompt-match twin found | 19 | Relink `stimulus_id` to the populated row |
| Single populated option in cluster (safe inherit) | 11 | Relink `stimulus_id` to that option |
| Multi-chart cluster, no prompt-match twin (ambiguous) | 4 | Retire — can't guess the correct chart |
| No populated option anywhere (unrecoverable) | 17 | Retire — chart never extracted |

Net: **30 questions relinked, 21 retired** from the empty-stim pool.

### Null-stim figure-referencers

48 live questions with `stimulus_id IS NULL` whose prompt contains an
unambiguous figure reference ("in the figure above", "shown above", "graph
below", etc.):

| Source | Count |
|---|---|
| `princeton_2012` | 44 |
| `kaplan_2024` | 3 |
| `manhattan_5lb_2018` | 1 |

These were designed around a figure that was never stored in the question
bank — text-only they are unanswerable. **All 48 retired.**

## UI layer — no changes required

`MathView.set_content` already produces full-width `WebView` output with
KaTeX, base64 PNG `<img>`, inline styles, and HTML tables. The passage panel
shows whatever `stimulus.content` contains. Confirmed by reading:

- `screens/question_screen.py` (passage panel at line 77-86, `set_content`
  call at line 328)
- `widgets/math_view.py` (`wx.html2.WebView` at line 243, `SetPage` at
  line 288)
- `widgets/html_sanitizer.py` (`img` in `ALLOWED_TAGS`, `data` in
  `ALLOWED_PROTOCOLS`)

## Fixes applied (feature branch `di-chart-integrity-2026-04-28`)

1. **Sub-fix A — data relink**: 30 live questions' `stimulus_id`
   repointed to the populated replacement. `review_notes` updated with the
   original-vs-new stim id for traceability.
2. **Sub-fix B — retire empty-stim orphans**: 21 live questions set to
   `status='retired'` (17 truly unrecoverable + 4 ambiguous).
3. **Sub-fix C — retire null-stim figure-referencers**: 48 live questions
   set to `status='retired'`.
4. **Sub-fix D — leave empty stimulus rows in place**: referential
   integrity. Retired questions still reference them; dead rows are harmless.

## Verification

- User's exact Q1 (Q4646, "In 1994, the total production … combined production
  of balls, bags, and gift items in the United States") now points to
  stimulus 468, which carries the HTML table "Golf Equipment and Supplies
  Production by Country and Category, 1994". The Passage pane will render
  this table on reload.
- All three live sibling questions on the golf cluster (Q4645/4646/4647)
  were relinked.

## Post-fix live-pool size

Before: 2 990 live. After: 2 990 − 69 retired = **2 921 live**. Drop: 2.3 %,
well under the 30 % stop threshold.
