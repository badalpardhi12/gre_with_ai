# GRE Exam-Mode UI Replication Spec — 2026-06

> **⚠️ PARTIALLY SUPERSEDED (2026-06-17).** This was the FIRST UI spec and
> describes a navy header / footer-navigator "sandwich". The app was later
> reworked to the real ETS **"Test Preview Tool"** scheme (charcoal header +
> maroon rule + top-right tool ribbon + pink section bar + black-bordered white
> box on a gray page; **no** bottom numbered navigator). For the CURRENT chrome,
> palette, and layout, read the implementation: `widgets/exam_chrome.py`,
> `widgets/exam_tool_button.py`, `widgets/theme.ExamColor` (the `# ETS "Test
> Preview Tool" scheme` block), `screens/question_screen.py`, plus
> `docs/PROJECT_STATUS.md`. The SUBSTANCE below is still valid and in use:
> per-question-type directions strings, control shapes (oval/square), the
> calculator key set + behaviors + Transfer Display, numeric-entry fraction box,
> figures-not-to-scale rules, and "exam mode for all sessions, fullscreen". Treat
> the navy/footer-navigator/colors sections as historical.

Implementation-grade spec for a faithful ETS GRE General Test (post-Sept-2023 / POWERPREP) test-taking interface inside the wxPython app at this repo. This describes a dedicated **"exam mode"** skin for the in-test screens (`QuestionScreen`, `ReviewScreen`, `InstructionsScreen`, calculator, timer, navigator) — distinct from the dark study-app chrome used everywhere else.

**Confidence legend** used throughout:
- **[C] CONFIRMED** — stated by ETS or corroborated by ≥2 reputable prep sources, or directly visible in the user-provided official screenshot.
- **[A] APPROX** — a visual-match hex/metric read off the screenshot; ETS does not publish exact values. Treat as a tuning target, not a constant.
- **[I] INFERRED** — reconstructed from design language / behavior descriptions; not an ETS-published value.

ETS does **not** publish brand or UI hex codes, pixel metrics, or fonts. Every hex/size below is either [A] (from the screenshot) or [I] (design inference). All *structural and behavioral* facts (control shapes, directions strings, calculator behavior, review columns, timer hide-not-off) are [C].

---

## 0. Scope & mode model

- Exam mode applies the ETS light skin only to the in-test flow screens. The rest of the app stays on the dark study theme.
- Recommended gating (see open questions in the plan): turn exam mode **on** for `mode == "simulation"` sessions (full mock + section test), and **off** for `mode == "learning"` drills (which keep Show Answer / Ask AI Tutor and can stay in the chromed study UI). This keeps the ETS skin "faithful where it matters" without amputating learning affordances.
- Exam mode additionally: hides the app sidebar, hides study-only buttons (Show Answer, Ask AI Tutor, Report, Review All, Exit to Dashboard collapse into ETS equivalents), and (optionally) takes over the window via `ShowFullScreen`.

---

## 1. Palette — ETS "exam" tokens

Add a parallel light palette. Two options (see plan §B): a sibling `ExamColor` class in `widgets/theme.py`, or a runtime-swappable active palette. Names below are the token contract.

| Token | Hex | Source | Semantic |
|---|---|---|---|
| `EXAM_HEADER_NAVY` | `#16284d` | [A] | Top header bar + bottom footer bar (the navy "sandwich") |
| `EXAM_HEADER_NAVY_HOVER` | `#1f3463` | [I] | Hover state on navy-bar controls |
| `EXAM_CONTENT_BG` | `#ffffff` | [C] white / [A] exact | Content area background |
| `EXAM_CONTENT_BG_ALT` | `#f7f7f7` | [I] | Subtle zebra / panel fill (e.g. RC passage pane) |
| `EXAM_TEXT` | `#000000` | [C] black on white | Question/answer body text |
| `EXAM_TEXT_MUTED` | `#444444` | [I] | Secondary/metadata text |
| `EXAM_TEXT_ON_NAVY` | `#ffffff` | [A] | Text/glyphs in header & footer |
| `EXAM_DIRECTIONS_BAND` | `#e6e6e6` | [A] | Full-width directions band fill |
| `EXAM_DIRECTIONS_TEXT` | `#1a1a1a` | [I] | Directions band text (sans) |
| `EXAM_DIVIDER` | `#cccccc` | [I] | Hairline rules between content regions |
| `EXAM_BTN_NEXT_BLUE` | `#2d8cff` | [A] | Primary "Next" button fill (dominant blue) |
| `EXAM_BTN_NEXT_BLUE_HOVER` | `#1f78e6` | [I] | Next hover/pressed |
| `EXAM_BTN_GREY` | `#5a5a5a` | [A/I] | Mark / Back dark-grey buttons |
| `EXAM_BTN_GREY_HOVER` | `#6e6e6e` | [I] | Mark / Back hover |
| `EXAM_BTN_TEXT` | `#ffffff` | [I] | Text on grey/blue buttons |
| `EXAM_SUBMIT_MAUVE` | `#9b8aa3` | [A] | "Submit Section" header button (muted mauve/grey) |
| `EXAM_SUBMIT_MAUVE_HOVER` | `#ab9bb3` | [I] | Submit Section hover |
| `EXAM_OVAL_BORDER` | `#555555` | [I] | Radio (oval) unselected outline |
| `EXAM_OVAL_FILL_SELECTED` | `#16284d` | [I] | Radio selected fill (navy dot) |
| `EXAM_CHECK_BORDER` | `#555555` | [I] | Checkbox (square) unselected outline |
| `EXAM_CHECK_FILL_SELECTED` | `#16284d` | [I] | Checkbox checked fill |
| `EXAM_TC_HIGHLIGHT` | `#cfe2ff` | [I] | Text-Completion selected-word highlight (light blue) |
| `EXAM_SELECT_IN_PASSAGE_HL` | `#fff3b0` | [I] | Select-in-passage selected sentence highlight (pale yellow) |
| `EXAM_ROW_HOVER` | `#eef3fb` | [I] | Answer-row hover tint |
| `EXAM_ROW_SELECTED` | `#dde9fb` | [I] | Answer-row selected tint |

### Navigator circle states (footer 1..N) — [C] semantics, [I] colors

| State | Fill | Border | Glyph | Source |
|---|---|---|---|---|
| Current | `#2d8cff` ring (2px) on white | `#2d8cff` | number | [C] state / [I] color |
| Answered | `#16284d` (filled navy) | `#16284d` | white number | [C] state / [I] color |
| Unanswered / Skipped | `#ffffff` (open) | `#888888` | dark number | [C] state / [I] color |
| Marked (flag overlay) | overlay on top of any of the above | — | small flag/check glyph `#d98b00` corner badge | [C] orthogonal axis / [I] glyph+color |

Marked is an **independent axis** — a circle may be Answered+Marked or Skipped+Marked [C].

### Timer warning colors (keep current behavior, re-point to readable-on-navy)

| Condition | Hex | Source |
|---|---|---|
| Normal (>5:00) | `#ffffff` on navy footer | [I] |
| ≤5:00 (reappears, warning) | `#ffd24d` (amber) | [I] (5:00 reappear is [C]) |
| ≤1:00 | `#ff6b6b` (light red, legible on navy) | [I] |

---

## 2. Typography

- **Item content (question stems, answer choices, QC quantities, RC passages, DI labels): SERIF.** [C] (GRE has always rendered item text in a Times-like serif.)
  - Web-safe / wx stack: prefer **Georgia** (more legible on screen), fall back to **"Times New Roman", Times, serif**. [I] design call; the family being serif is [C].
  - For the WebView (`math_view.py`) CSS: `font-family: Georgia, "Times New Roman", Times, serif;`
  - For native wx widgets: build a `wx.Font(..., wx.FONTFAMILY_ROMAN, ...)` and `SetFaceName("Georgia")` (falls back to platform serif if absent).
- **UI chrome / button labels / directions band / section-question counters / navigator numbers / timer: SANS-SERIF.** [C] (only item content is serif; chrome is sans).
  - Stack: `-apple-system, "Helvetica Neue", Arial, sans-serif` (WebView) / `wx.FONTFAMILY_DEFAULT` (native). [I]
- **Calculator display**: monospace LCD-style digits (keep current `wx.FONTFAMILY_TELETYPE`). [I]
- **Sizes** ([I], tune against screenshot; route through `widgets/ui_scale.py` so Cmd-+/- and DPI still work — current code hardcodes these and bypasses `ui_scale`):
  - Question stem: ~16pt serif.
  - Answer choices: ~15pt serif.
  - Section/Question counter: ~13pt bold sans.
  - Directions band: ~12pt sans.
  - Navigator numbers: ~10pt sans.
  - Timer: ~14pt sans (footer), monospace optional.
- Math: KaTeX keeps its bundled Computer Modern serif; only `$$…$$`, `\(…\)`, `\[…\]` delimiters (single `$` is **not** a delimiter — project constraint).

---

## 3. Layout per region

The screen is a navy header + white content area + navy footer "sandwich" [C].

### 3.1 Header bar (top, full width, ~50–60px [I], `EXAM_HEADER_NAVY`)
- **Left:** ETS logo — white rounded oval/pill containing "ETS", followed by white **italic "GRE"** wordmark. [C] brand lockup / [I] exact rendering. Render as a small static bitmap or owner-drawn pill + italic StaticText in `EXAM_TEXT_ON_NAVY`.
- **Right:** **"Submit Section"** button — muted mauve/grey (`EXAM_SUBMIT_MAUVE`), rounded, with a small **up-arrow** glyph, right-most control. [C] label & function / [A] color. Function = leave/finalize current section (older builds call it "Exit Section"; current label is "Submit Section" [C]). Wire to the existing end-of-section path.

### 3.2 Section / question label (top-left of white content area)
- Two bold dark-sans lines/segments: **"Section X of Y"** and **"Question N of M"**. [C]
- **Y = 5** scored sections (1 AWA + 2 Verbal + 2 Quant); a real test may show "of 6" with an unscored research section. Use 5. [C]
- **M = 12** for Section-1 of a measure, **15** for Section-2 — for BOTH Verbal and Quant. NOT 20. [C] (the screenshot's "15 of 20" is the pre-2023 format; do not copy it.)
- Current code formats this as `"Quantitative Reasoning — Section 2"` + `"Question N of M"` (`question_screen.py:236,314`). Reformat to ETS `"Section X of Y"` / `"Question N of M"`. Section *name* ("Verbal Reasoning"/"Quantitative Reasoning") may still appear; ETS leads with the numeric "Section X of Y".

### 3.3 Content area (white, `EXAM_CONTENT_BG`, black serif text)
- Question stem at top.
- For RC/DI: split into independently scrolling **left stimulus pane** + **right question pane** (see §5.7/§5.8). The current `SplitterWindow` (`question_screen.py:72-130`) already does passage/question split — re-skin, keep.
- Answer controls left-aligned, choice text to the right (current label-less control + StaticText row pattern at `question_screen.py:557-603` is good for click-target parity — re-skin, keep). [C] layout / [I] exact metrics.

### 3.4 Directions band (full-width, between content and nav row, `EXAM_DIRECTIONS_BAND`)
- A distinct full-width light-grey band, centered sans-serif directions text. [C] presence / [A] color. Replaces the current one-line italic grey `subtype_label` (`question_screen.py:107-111,318-331`).
- **Exact ETS directions strings by question type** (all [C] on substance; ETS standard wordings):

  **Verbal**
  - RC / MC single-answer: **"Select one answer choice."** (micro-prompt under choices, optional: **"Click on your choice."**)
  - RC select-one-or-more (3 choices, 1–3 correct): **"Consider each answer choice separately and select all that apply."** (often shortened on screen to **"Select all that apply."**)
  - Select-in-Passage: **"Select the sentence in the passage that "** + action prompt **"Click on a sentence in the passage to select it."** ([C] behavior; exact micro-string [I].)
  - Text Completion, 1 blank / 5 choices: **"Select one entry for the blank from the corresponding column of choices. Fill the blank in the way that best completes the text."**
  - Text Completion, 2–3 blanks / 3 choices each: **"Select one entry for each blank from the corresponding column of choices. Fill all blanks in the way that best completes the text."**
  - Sentence Equivalence: **"Select the two answer choices that, when used to complete the sentence, fit the meaning of the sentence as a whole and produce completed sentences that are alike in meaning."** (commonly summarized on screen as **"Select exactly two answer choices."**)

  **Quant**
  - Quantitative Comparison — the four fixed choices, verbatim and always in this order:
    1. "Quantity A is greater."
    2. "Quantity B is greater."
    3. "The two quantities are equal."
    4. "The relationship cannot be determined from the information given."
  - MC single: **"Select one answer choice."**
  - MC one-or-more: **"Select one or more answer choices."** / fixed count: **"Select [exactly N] answer choices."**
  - Numeric Entry (single box): **"Enter your answer as an integer or a decimal in the answer box."**
  - Numeric Entry (fraction, two boxes): **"Enter your answer as a fraction. There is one box for the numerator and one box for the denominator."**
  - Quant section caveat (in section directions): **"Figures are not necessarily drawn to scale."** (graphs/coordinate systems ARE to scale). [C]
  - Numeric-entry input rule: do not enter symbols like %, $, /, or commas; a unit/currency label may sit beside the box. [C]

### 3.5 Navigation row (centered, above footer)
- Three buttons, **left→right: Mark, Back, Next**. [C]
  - **Mark** — `EXAM_BTN_GREY`, **checkbox icon**, toggles flag for current question without affecting answer. [C]
  - **Back** — `EXAM_BTN_GREY`, **left-arrow ◀**. [C]
  - **Next** — `EXAM_BTN_NEXT_BLUE` (dominant), **right-arrow ▶**. [C]
- Free movement within a section: skip / Back / Next / edit any answer while time remains. [C]
- No return to a prior section once submitted (section-level adaptive). [C]
- Pressing Next on the last question (or Submit Section) → section Review screen (§6). [C]
- Maps to current footer buttons `prev_btn`/`next_btn` (`question_screen.py:177-183`); `mark_btn` (`:146`) moves into this row; study-only buttons (Show Answer, Ask AI Tutor, Report, Review All, Exit to Dashboard) are hidden in exam mode.

### 3.6 Footer bar (bottom, full width, `EXAM_HEADER_NAVY`)
Left→right [C] (screenshot):
- **"Go to Question:"** label + small numeric input — type a number to jump. [C]
- **Help (?) icon.** [C]
- **"Hide Progress" button** — toggles the row of numbered circles on/off (flips to "Show Progress"). [C]
- **Row of round numbered circles (1..N)** — per-question status navigator (states in §1). [C]
- Bottom-right cluster: **"Calc"** (grey, Quant sections only — opens/closes calculator); **"Help"** (grey); **timer** "H:MM:SS"; **"Hide Time"** toggle. [C]
- This replaces the current pastel rectangular `QuestionNav` (`widgets/question_nav.py`) and the footer button bar. The current `QuestionNav` already supports `set_state(current, answered, marked)` and `set_on_navigate` — keep the API, swap the rendering to navy-footer round circles.

### 3.7 Timer + Hide Time
- Placement: bottom-right of footer. [C]
- Format: **H:MM:SS** counting **down** remaining section time, leading "0:" hour digit for sub-hour values (e.g. `0:11:42`). [C]
- **"Hide Time" toggle**: can hide but **cannot turn off**; label flips Hide Time ↔ Show Time. [C]
- Auto-reappears at **5:00** remaining as a warning; reportedly cannot be re-hidden after that. [C] reappear / [I] exact 5:00 + can't-re-hide.
- On expiry: section auto-submits/advances. [C]
- Current `TimerWidget` (`widgets/timer.py`) has NO hide/show — add it. Re-point its hardcoded red/orange/system colors to the navy-legible warning colors in §1.

---

## 4. On-screen calculator spec (Quant only)

Re-skin/extend `widgets/calculator.py`. Confidence as tagged.

### 4.1 Key set [C]
Digits `0–9`, `.`, `+`, `−`, `×`, `÷`, `=`, `√`, `(`, `)`, `±`, `C`, `CE`, memory `MR`/`MC`/`M+`, and **Transfer Display**.
- **Exactly THREE memory keys** (`MR`, `MC`, `M+`), one memory slot, `M` indicator at far-left of display. **No `MS`, no `M−`.** [C]
- **NO `%` key.** [C] (Current code's docstring claims `%` but no button exists — keep it absent; that is correct.)
- **NO** exponent/power, `π`, `e`, log/ln, trig. [C]
- Two-key clear pair: `C` (clear all/dismiss ERROR) + `CE` (clear entry). Current code has only `C` — add `CE`. [C]
- Digit layout = **phone keypad** (1-2-3 top, 7-8-9 lower, 0 bottom). [C]

### 4.2 Layout (contents [C], exact cell mapping [I])
```
[ display: 8 digits, right-aligned;  "M" indicator at far left when memory in use ]
Row 1:   MR    MC    M+                  ← memory row (top)
Row 2:   CE    C     ±     √             ← clear/utility row
Row 3:   7     8     9     ÷
Row 4:   4     5     6     ×
Row 5:   1     2     3     −
Row 6:   0     .     =     +
Row 7:   (     )                          ← parentheses pair
[ Transfer Display ]                      ← full-width bar at the very bottom
```
Reliable visual anchors: memory keys in a top row; Transfer Display a single wide button at the very bottom. [C]
Current code uses a 6×4 grid with `√ ( ) =` on the last row (`calculator.py:14`); rework to the above (memory row on top, add `CE`, parentheses pair, keep Transfer Display).

### 4.3 Behaviors
- **PEMDAS / precedence** (NOT left-to-right): parentheses → roots/exponent → ×÷ (L→R) → +− (L→R). ETS example `1 + 2 × 4 = 9`. [C] The paper test is left-to-right; the on-screen test is precedence — implement precedence. (Current code uses Python `eval` after whitelisting, which already gives PEMDAS — keep that property, but see safety note.)
- **Single-level parentheses only** — no nesting; `=` can force a sub-result mid-expression. [C]
- **`√` is postfix/unary** — enter operand, press `√`, display becomes its square root. [C]
- **`±`** toggles the sign of the current display value; keyboard `-` is the subtract operator only (no keyboard sign-change). [C]
- **8-digit display**, American thousands separators (comma every 3 digits). [C]
- **`ERROR`** (all caps) on: ÷0, √(negative), result > 99,999,999. Only `C` dismisses ERROR; display locked until then. [C]
- A positive result `< 10⁻⁷` displays as `0` but the **true internal value is retained** for the next operation. [C] (keep full internal precision; clamp only the rendered string).
- Non-terminating results rounded to fit 8 digits. [C]
- Memory: `M+` accumulates display into memory (does not overwrite), lights `M`; `MR` recalls; `MC` clears + removes `M`; `C` clears display but leaves memory + `M` intact. [C]
- **Keyboard shortcuts** when calculator focused: `0–9 . + - * / ( ) =` and Enter. No shortcut for `± √ C CE` or backspace (backspace does not clear). [C]
- Safety: current code's `eval` rejects `**` and whitelists chars — keep/strengthen; precedence is fine but ensure single-level-paren constraint and 8-digit/ERROR clamping wrap the result.

### 4.4 Transfer Display rules [C unless noted]
- Copies the **current display value verbatim** (no rounding/reformatting) into the question's single answer box. [C]
- **Enabled ONLY on single-box Numeric Entry** questions. Grayed/disabled on: QC, all MC, and **fraction-form** Numeric Entry (two boxes). [C]
- Visual: disabled = light-grey/low-contrast, non-clickable; enabled = darker grey, clickable. Focus shows white outline on the button + blue outline on the whole calculator. [C] behavior / [I] shades.
- `C`/`CE` on the calculator does NOT clear an already-transferred answer-box value. [C]
- Current `set_on_transfer(callback)` → `_on_transfer` (`calculator.py:173`) already copies the display out; add the per-question enable/disable gating tied to question subtype.

### 4.5 Window behavior
- Draggable floating window, show/hide at will during Quant, compact, light-grey body with darker-grey bevel keys, light LCD display with dark digits, blue focus outline. [C] behavior / [I] exact shades. (Current widget is created hidden and toggled by `calc_btn` — keep that toggle; make it a floating draggable frame for fidelity, or keep inline panel if simpler — see plan.)

---

## 5. Per-question-type layout

All controls follow ETS: **oval radio = single-select**, **square checkbox = multi-select**. [C]

### 5.1 Quantitative Comparison (QC) [C]
- Optional **common information** centered above, applying to both quantities. [C]
- Two columns: **Quantity A** (left) / **Quantity B** (right), header labels emphasized/underlined. [C labels & two-column / I underline].
- Four fixed choices below the columns (full-width, not inside columns), each preceded by an **OVAL radio** (single-select), wording/order verbatim per §3.4 QC list. [C]
- Maps to current `qc` subtype (`question_screen.py:450-461`, radio buttons) — re-skin to ovals, add the two-column quantity header rendering (new; current code renders QC as plain MC radios with no A/B columns).

### 5.2 MC — Select One [C]
- Exactly 5 choices, **OVAL radio**, single-select; selecting another moves the selection. [C]
- Maps to `mcq_single`, `rc_single`, `data_interp` (`question_screen.py:450-461`).

### 5.3 MC — Select One or More [C]
- Each choice preceded by a **SQUARE checkbox** (multi-select; click again to clear). [C]
- Quant: count varies, may or may not be stated. RC: exactly 3 choices, 1–3 correct. [C]
- Keep the live "Your selections" indicator behavior if desired (study aid), but ETS itself does not show it — consider hiding in exam mode. Maps to `mcq_multi`, `rc_multi` (`question_screen.py:463-502`).

### 5.4 Sentence Equivalence (SE) [C]
- Single sentence, one blank, **6 choices**, select **exactly 2**, **SQUARE checkboxes** (cap selection at 2). [C]
- Maps to `se` (`question_screen.py:463-502`) — already checkboxes; cap at 2 and re-skin to ETS squares.

### 5.5 Numeric Entry [C]
- **Single value:** ONE answer box (`wx.TextCtrl`); integer/decimal via keyboard. [C]
- **Fraction:** TWO **stacked** boxes (numerator on top, denominator below) separated by a horizontal **fraction bar**. [C] (Current `numeric_entry.py` fraction mode renders numerator `/` denominator **side-by-side** with a " / " label — change to a **stacked** numerator-over-denominator with a drawn horizontal bar for fidelity.)
- **Unit/currency labels** sit adjacent to the box — a "$" may print to the **left**, a unit word ("feet") to the **right**; user types only the number. [C] Add an optional adjacent static label driven per-question.
- No symbols typed into boxes (no %, $, /, commas). [C]
- Transfer Display enabled only for the single-box form (§4.4). [C]
- Maps to `numeric_entry` (`question_screen.py:533-548`, `widgets/numeric_entry.py`).

### 5.6 Text Completion (TC) [C]
- 1–5 sentence passage, 1–3 blanks. [C]
- 1 blank → **5 choices** in a single vertical column. 2–3 blanks → **3 choices per blank**, laid out as a **table of columns**, one column per blank, labeled **Blank (i) / (ii) / (iii)**. [C]
- **Selection mechanic = highlight** (NOT oval/checkbox): click a choice word to highlight/select it; misclick → click a different word to move the highlight. [C] Use `EXAM_TC_HIGHLIGHT` background on the selected choice per blank.
- No partial credit. [C]
- Current `tc` (`question_screen.py:504-531`) renders `Blank N:` headers + radio buttons per choice. For fidelity, change to per-blank columns labeled `Blank (i)/(ii)/(iii)` and a highlight-on-click selection (or keep radios as an acceptable approximation — flag as a fidelity gap).

### 5.7 Reading Comprehension (RC) [C]
- **Two-pane:** passage in a **LEFT pane that scrolls independently**; question + choices on the **RIGHT**. [C] (Current splitter already does this — `question_screen.py:72-130,334-361`.)
- RC hosts three subtypes sharing the left passage:
  - Select One → 5 oval radios. [C]
  - Select One or More → 3 square checkboxes, 1–3 correct. [C]
  - **Select-in-Passage** → no choices on the right; click any word in a sentence in the LEFT pane and the **whole sentence highlights** (`EXAM_SELECT_IN_PASSAGE_HL`). Clickable region may be restricted to specified paragraph(s); clicks elsewhere do nothing. [C] Current `rc_select_passage` (`question_screen.py:413-448`) renders sentences as radio buttons on the right — for fidelity this should become clickable highlightable sentence spans in the left passage pane (significant rework; flag as gap — radio-list is an acceptable interim approximation).

### 5.8 Data Interpretation (DI) [C]
- A **shared stimulus** (table/bar/line/circle graph) with **multiple consecutive questions** keyed to it. Graphs ARE to scale (estimation valid). [C]
- Member questions: Select One (oval) / Select One or More (square) / Numeric Entry. [C]
- Layout: reuse the RC two-pane idea — stimulus persists in left pane across the set. [I] DI questions are kept consecutive in a section already (per repo commit `6668b80`).
- Maps to `data_interp` (`question_screen.py:450-461`).

---

## 6. End-of-section Review screen [C]

Reached via Next-on-last-question or "Submit Section"/Review. Re-skin `screens/review_screen.py`.
- A **table, one row per question** in the current section. [C]
- **Columns (live test):** **Question Number** | **Status** | **Marked**. [C]
  - **Status** ∈ { **Answered**, **Not Answered** (skipped), **Not Seen** (not viewed), **Incomplete** (partially-answered multi-select) }. [C]
  - **Marked** = flag/check glyph showing whether Mark-flagged. [C]
- **OMIT a "Score Status" / correctness column for live-test fidelity** — correctness is not shown mid-section on the real test. Gate any correctness column behind a practice/learning mode only. [C]
- **Navigation:** a **"Go to Question"** input (type a number to jump), a **"Return"** button (back to the question you were on), and clicking a row jumps to that item. [C]
- A legend/instructions line at top explaining the check marks and "Incomplete". [C]
- Header label: "Review Your Answers" / "Section Review" ([I] exact title; "Review" is the button).
- Current `ReviewScreen` is a `wx.ListCtrl` of #/Status/Marked with "Go to Selected Question" / "Return to Questions" / red "End Section" — close already; reword to ETS columns/labels, add "Go to Question" numeric jump, add Not-Seen/Incomplete statuses, ensure no correctness column in simulation mode.

---

## 7. Section instructions screen [I/C]

`screens/instructions_screen.py`. ETS shows a section-intro page before each section with the section's directions/rules (count, time, navigation rules, figures-not-to-scale caveat for Quant). Re-skin to the ETS light frame with the navy header + a "Continue"/"Begin" affordance. Use the §3.4 directions substance. Current screen has a dark `BG_PAGE` centered title + body + Cancel/Begin row — re-skin for exam mode; keep the `display_label` override path for mixed drills.

---

## 8. What stays sans / what goes serif (summary)

| Element | Font |
|---|---|
| Question stem, answer choices, QC quantities, RC/DI passages & labels | **Serif** (Georgia / Times) [C] |
| Math (KaTeX) | KaTeX Computer Modern serif [C] |
| Header logo/wordmark, Submit Section | Sans (italic GRE wordmark) [C] |
| Section/Question counter | Sans bold [C] |
| Directions band | Sans [C] |
| Mark/Back/Next labels | Sans [C] |
| Footer: Go to Question, Hide Progress, Calc, Help, timer, navigator numbers | Sans [C] |
| Calculator digits | Monospace [I] |

---

## 9. Faithful values to hard-code (quick checklist)

- Header & footer navy `#16284d` [A]; content white; item text black serif (Georgia/Times) [C].
- Submit Section: mauve/grey `#9b8aa3` [A] w/ up-arrow, top-right.
- Directions band: full-width `#e6e6e6` [A], centered sans, per-type strings from §3.4 [C].
- Buttons centered, order **Mark (grey) · Back (grey ◀) · Next (blue `#2d8cff` ▶)** [C/A].
- Counters: "Section X of 5" + "Question N of M", **M = 12 (sec 1) or 15 (sec 2)** for both measures [C] — NOT 20.
- Footer: Go-to-Question input · ? help · Hide Progress toggle · circles(1..N) · Calc (Quant only) · Help · timer H:MM:SS countdown · Hide Time toggle [C].
- Navigator states: Current (blue ring) / Answered (filled navy) / Unanswered (open) / Marked (flag overlay, independent axis) — colors [I].
- Calculator: 3 memory keys, single slot, M indicator; no `%`/exponent/trig; PEMDAS + single-level parens; 8-digit + commas; `ERROR` on ÷0/√neg/overflow (only `C` dismisses); `0` for `<10⁻⁷` keeping true value; `±` sign (no keyboard); `√` postfix; Transfer Display only on single-box NE [C].
- Rules: free movement + edit within a section; section closes on submit; no return to prior section; timer hideable not stoppable, reappears ~5:00 [C].

---

## 10. Confidence summary

- **CONFIRMED (ETS / reputable):** section structure & timing (12/15-per-section), all per-type directions strings & control shapes (oval vs square, fraction two-box stacked, TC columns, two-pane RC, select-in-passage), Mark/Review semantics, free-within-section movement, no prior-section return, full calculator key set + behaviors + Transfer Display gating, timer hide-not-off + H:MM:SS countdown + 5:00 reappear, review columns (Question/Status/Marked, no live correctness).
- **APPROX (screenshot visual-match):** header/footer navy `#16284d`, Submit Section mauve `#9b8aa3`, Next blue `#2d8cff`, directions band `#e6e6e6`, "Submit Section" wording, footer layout.
- **INFERRED (design language):** all other hexes, every pixel/point metric, navigator circle colors & marked glyph, hover/pressed states, exact serif face choice (Georgia), select-in-passage micro-prompt wording, calculator window shades.
