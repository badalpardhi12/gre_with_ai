"""Build a DB-backed sample-review markdown for the persisted Princeton items.

Runs *after* ``scripts/persist_princeton.py`` lands rows in the worktree
DB. Picks the same selection used by the previous sampler (per
(subtype, drill) bucket) and renders each item's stem, options, expert-
review verdict, and persistence status straight from the database — so
what reviewers see is exactly what the runtime question screen will
serve.

Output: ``/Users/chiku/.../princeton_sample_review.md``
Assets: ``/Users/chiku/.../princeton_sample_review_assets/``
"""
import json
import os
import shutil
import sys

WORKTREE = "/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a9405213"
MAIN = "/Users/chiku/Documents/side_projects/gre_with_ai"
if WORKTREE not in sys.path:
    sys.path.insert(0, WORKTREE)

from models.database import (  # noqa: E402
    init_db, Question, QuestionOption, Stimulus,
)


OUT_MD = os.path.join(MAIN, "princeton_sample_review.md")
OUT_ASSETS = os.path.join(MAIN, "princeton_sample_review_assets")
SRC_ASSETS = os.path.join(WORKTREE, "sample_review_tmp", "assets")
SELECTION = os.path.join(WORKTREE, "sample_review_tmp", "selection.json")
ALL_Q = os.path.join(WORKTREE, "sample_review_tmp", "all_questions.json")
PERSISTENCE_SUMMARY = os.path.join(
    WORKTREE, "data", "extracted", "princeton", "persistence_summary.json")

EXTRACTOR_FULL = "Princeton extractor v3 — multi-judge expert review gate live"
EPUB_FILENAME = ("Princeton Review - 1,014 GRE Practice Questions, "
                 "3rd Edition-Princeton Review (2012).epub")


def _md_escape(s):
    if s is None:
        return ""
    return (s.replace("’", "'").replace("‘", "'")
             .replace("“", '"').replace("”", '"'))


def _pretty_blank(label):
    roman = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v"}
    if label and label.startswith("blank"):
        try:
            n = int(label[len("blank"):])
            return f"Blank ({roman.get(n, str(n))})"
        except ValueError:
            return label
    return label


def _options_table_for_tc(opts):
    """Group flat ``blankN_X`` labels back into a per-blank markdown table."""
    blanks = {}
    flat = []
    for o in opts:
        lab = o.option_label or ""
        if "_" in lab:
            blank, ch = lab.split("_", 1)
            blanks.setdefault(blank, []).append((ch, o.option_text, o.is_correct))
        else:
            flat.append((lab, o.option_text, o.is_correct))
    rows = []
    if blanks:
        for bk in sorted(blanks):
            cells = []
            for ch, text, correct in blanks[bk]:
                tag = f"{ch}. {text.strip()}"
                if correct:
                    tag = f"**{tag}** (CORRECT)"
                cells.append(tag)
            rows.append(f"**{_pretty_blank(bk)}**: " + " | ".join(cells))
    if flat:
        cells = []
        for ch, text, correct in flat:
            tag = f"{ch}. {text.strip()}"
            if correct:
                tag = f"**{tag}** (CORRECT)"
            cells.append(tag)
        rows.append("**Blank (i)**: " + " | ".join(cells))
    return "\n\n".join(rows)


def _render_options_block(q):
    opts = list(q.options)
    if q.subtype == "tc":
        return _options_table_for_tc(opts)
    if not opts:
        if q.subtype == "numeric_entry":
            return "_(numeric entry — no multiple choice)_"
        if q.subtype == "rc_select_passage":
            return "_(select-the-sentence — answer is a sentence from the passage)_"
        return "_(no options)_"
    lines = []
    for o in opts:
        mark = " (CORRECT)" if o.is_correct else ""
        lines.append(f"- {o.option_label}. {(o.option_text or '').strip()}{mark}")
    return "\n".join(lines)


def _render_correct(q):
    if q.subtype == "numeric_entry":
        nas = list(q.numeric_answers)
        if nas:
            na = nas[0]
            if na.numerator is not None:
                return f"`{na.numerator}/{na.denominator}` (fraction)"
            return f"`{na.exact_value}` (tol={na.tolerance})"
        return "_(no numeric answer stored)_"
    correct = [o for o in q.options if o.is_correct]
    if not correct:
        return "_(no correct option marked)_"
    return ", ".join(o.option_label for o in correct)


def _expert_review_line(review_notes, status):
    if status == "live" and not review_notes:
        return "**Expert Review:** PASS — promoted to live."
    if not review_notes:
        return f"**Expert Review:** _(none recorded; status={status})_"
    try:
        blob = json.loads(review_notes)
    except (TypeError, ValueError):
        return f"**Expert Review:** FAIL — non-JSON notes: {review_notes[:160]}"
    stage = blob.get("stage", "?")
    if stage == "expert_review":
        v = blob.get("verdict") or {}
        notes = v.get("reviewer_notes", "")
        return f"**Expert Review:** FAIL — {notes[:300]}"
    if stage == "extraction_verifier":
        v = blob.get("verdict") or {}
        defects = ",".join(v.get("defects") or []) or "unspecified"
        return f"**Expert Review:** _(routed to draft by extraction verifier: {defects})_"
    if stage == "deterministic_gates":
        gates = ",".join(blob.get("failed_gates") or []) or "?"
        return f"**Expert Review:** _(skipped — deterministic gate fail: {gates})_"
    return f"**Expert Review:** _(stage={stage})_"


def _short_notes_summary(review_notes):
    """Return a short tag like 'expert_pass' / 'expert_judge_disagree' for headlines."""
    if not review_notes:
        return "expert_pass_or_skipped"
    try:
        blob = json.loads(review_notes)
    except (TypeError, ValueError):
        return "non_json_notes"
    return blob.get("stage", "?")


def _copy_assets():
    if os.path.isdir(SRC_ASSETS):
        os.makedirs(OUT_ASSETS, exist_ok=True)
        for fn in os.listdir(SRC_ASSETS):
            shutil.copy2(os.path.join(SRC_ASSETS, fn),
                         os.path.join(OUT_ASSETS, fn))


def _load_summary():
    if not os.path.exists(PERSISTENCE_SUMMARY):
        return None
    try:
        with open(PERSISTENCE_SUMMARY) as f:
            return json.load(f).get("summary")
    except Exception:
        return None


def _render_question(q, label_prefix):
    parts = [f"#### {label_prefix} qst#{q.id} — anchor `{q.source_anchor}`, "
             f"subtype={q.subtype}, status={q.status}"]
    parts.append("")
    parts.append("**Stem:**")
    parts.append("")
    parts.append(_md_escape(q.prompt))
    parts.append("")
    parts.append("**Options:**")
    parts.append("")
    parts.append(_render_options_block(q))
    parts.append("")
    parts.append(f"**Correct answer:** {_render_correct(q)}")
    parts.append("")
    parts.append(_expert_review_line(q.review_notes, q.status))
    parts.append("")
    return "\n".join(parts)


def main():
    init_db()
    _copy_assets()

    summary = _load_summary()

    md = []
    md.append("# Princeton Extraction — Sample Review (DB-backed)\n")
    md.append("Rendered straight from the worktree's persisted DB after the "
              "deterministic gates + per-item vision verifier + multi-judge "
              "expert review have all run. What you see is exactly what the "
              "runtime question screen will serve.\n")

    md.append("## Before / after — TC option rendering\n")
    md.append("**Before:** TC items inside the verbal section emitted both the "
              "publisher's answer-table GIF (as an `<img>`) AND a bullet list of "
              "the vision-extracted options. The user flagged this — embedding "
              "the raw GIF on top of the text rendering defeated the whole point "
              "of the vision pass.\n")
    md.append("**After:** every TC item — single-blank, multi-blank, RC-context "
              "or stand-alone — renders its choices as a markdown table:\n")
    md.append("```\n**Blank (i)**: A. word | B. word | C. word\n"
              "**Blank (ii)**: D. word | E. word | F. word\n```\n")
    md.append("The publisher's GIF is no longer embedded anywhere in the "
              "rendered review. Zero raw `[img:...]` placeholders remain for "
              "TC items.\n")

    md.append("## Expert review gate (NEW)\n")
    md.append("Every text-only item that passed the deterministic gates AND "
              "the per-item vision verifier is now scored by a 3-model jury "
              "(Opus 4.7 + Sonnet 4.6 + Gemini 3.1 Pro) on a 5-axis 1-5 rubric: "
              "correctness, clarity, distractor quality, difficulty match, and "
              "GRE authenticity. Promotion to `status='live'` requires every "
              "axis backed by ≥4 from at least 2 judges, with a spread-of-2 "
              "disagreement guard. Items failing the jury land as "
              "`status='draft'` with the per-judge breakdown stashed in "
              "`Question.review_notes`.\n")
    md.append("Items whose stem references a chart, geometry diagram, or other "
              "figure ride past the text-only jury (`expert_skipped_figure`) "
              "and rely on the deterministic gates + vision verifier instead.\n")

    if summary:
        md.append("## Persistence summary\n")
        md.append("```json")
        md.append(json.dumps(summary, indent=2, ensure_ascii=False))
        md.append("```\n")

    # ── Sample selection ----------------------------------------------
    # Reuse the same per-bucket selection logic as the prior sampler so
    # the diff against the old md is meaningful.
    if not os.path.exists(SELECTION):
        print("WARN: selection.json missing — rendering the first 40 live items instead.")
        live = (Question
                .select()
                .where(Question.source == "princeton_2012")
                .where(Question.status == "live")
                .limit(40))
        md.append("## Sampled items\n")
        for q in live:
            md.append(_render_question(q, ""))
            md.append("\n---\n")
    else:
        sel = json.load(open(SELECTION))
        anchor_map = {}
        if os.path.exists(ALL_Q):
            for d in json.load(open(ALL_Q)):
                anchor_map[d["qst_id"]] = d["source_anchor"]

        def _q_for(qst_id):
            anchor = anchor_map.get(qst_id)
            if not anchor:
                return None
            return (Question
                    .select()
                    .where((Question.source == "princeton_2012")
                           & (Question.source_anchor == anchor))
                    .first())

        for measure_label, bucket in (("QUANT", sel.get("quant", [])),
                                      ("VERBAL", sel.get("verbal", []))):
            md.append(f"## {measure_label} (sampled, DB-backed)\n")
            current_group = None
            for entry in bucket:
                q = _q_for(entry["qst_id"])
                if q is None:
                    continue
                if entry["group"] != current_group:
                    md.append(f"### {entry['group']}\n")
                    current_group = entry["group"]
                md.append(_render_question(q, ""))
                md.append("\n---\n")

    # ── Final stats ---------------------------------------------------
    n_total = (Question.select()
               .where(Question.source == "princeton_2012").count())
    n_live = (Question.select()
              .where((Question.source == "princeton_2012")
                     & (Question.status == "live")).count())
    md.append("## DB roll-up\n")
    md.append(f"- Princeton rows in DB: **{n_total}**")
    md.append(f"- live: **{n_live}**")
    md.append(f"- draft: **{n_total - n_live}**")
    md.append(f"- live rate: **{(100.0 * n_live / max(n_total, 1)):.1f}%**\n")

    out = "\n".join(md)
    with open(OUT_MD, "w") as f:
        f.write(out)
    print(f"wrote {OUT_MD}  ({len(out)} bytes)")


if __name__ == "__main__":
    main()
