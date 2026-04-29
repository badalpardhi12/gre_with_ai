"""
Render the Phase-1 synthetic batch into a human-review markdown file.

Mirrors the structure of `princeton_sample_review.md` and
`kaplan_sample_review.md`:

- Top-of-file metadata (run id, model panel, drafted/promoted counts,
  per-axis means, cost estimate).
- Section headers (Quant first, then Verbal), then per-subtype.
- Per item:
  - Stem with LaTeX preserved verbatim.
  - Options with the correct one starred.
  - Subtopic + difficulty + pipeline metadata (rubric scores per axis,
    decision, revise rounds, solver verdict).
- RC clusters render the passage once + all its questions underneath.
- DI clusters render the chart once + all 3 questions underneath.
- Geometry items embed the SVG inline (we copy the asset out of the
  per-run audit dir into a sibling assets dir next to the markdown).

The renderer reads from the live DB (so we get the persisted candidate
rows) and is keyed by `run_id`.
"""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _q_summary_row(q) -> Dict[str, Any]:
    """Pull a Question row plus its options + stim into a flat dict."""
    from models.database import NumericAnswer, QuestionOption, Stimulus
    options = list(
        QuestionOption
        .select()
        .where(QuestionOption.question == q)
        .order_by(QuestionOption.option_label)
    )
    stim = None
    if q.stimulus_id:
        s = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
        if s:
            try:
                render = json.loads(s.render_spec or "{}")
            except (ValueError, TypeError):
                render = {}
            stim = {
                "type": s.stimulus_type,
                "title": s.title,
                "content": s.content,
                "render_spec": render,
            }
    numeric = None
    if q.subtype == "numeric_entry":
        n = NumericAnswer.get_or_none(NumericAnswer.question == q)
        if n:
            numeric = {
                "exact_value": n.exact_value,
                "numerator": n.numerator,
                "denominator": n.denominator,
                "tolerance": n.tolerance,
                "mode": n.mode,
            }
    try:
        provenance = json.loads(q.provenance_json or "{}")
    except (ValueError, TypeError):
        provenance = {}
    return {
        "qid": q.id,
        "measure": q.measure,
        "subtype": q.subtype,
        "topic": q.topic,
        "subtopic": q.subtopic,
        "stem": q.prompt,
        "explanation": q.explanation,
        "difficulty_target": q.difficulty_target,
        "options": [
            {"label": o.option_label, "text": o.option_text,
              "is_correct": o.is_correct}
            for o in options
        ],
        "numeric": numeric,
        "stimulus": stim,
        "provenance": provenance,
        "status": q.status,
        "run_id": q.run_id,
    }


def _format_options(opts: List[Dict[str, Any]]) -> str:
    lines = []
    for o in opts:
        marker = " (correct)" if o["is_correct"] else ""
        lines.append(f"- **{o['label']}.** {o['text']}{marker}")
    return "\n".join(lines)


def _format_axes(provenance: Dict[str, Any]) -> str:
    judge = provenance.get("judge") or {}
    medians = judge.get("medians") or {}
    if not medians:
        return "_No judge data._"
    rows = []
    for axis, m in medians.items():
        rows.append(f"  - {axis}: {m:.1f}")
    summary = (f"Mean {judge.get('mean', 0):.2f}, "
               f"min-axis {judge.get('min_axis', 0):.1f}")
    return f"{summary}\n" + "\n".join(rows)


def _format_solver(provenance: Dict[str, Any]) -> str:
    solver = provenance.get("solver")
    if not solver:
        return "_No solver data._"
    attempts = solver.get("attempts") or []
    if not attempts:
        return "_No solver attempts._"
    bits = []
    for a in attempts:
        ok = "✓" if a.get("matches_key") else "✗"
        bits.append(f"{a.get('solver','?')}: {ok} ({a.get('chose','?')})")
    return ", ".join(bits)


def _format_expert_review(provenance: Dict[str, Any]) -> str:
    """Render the expert-review block from the provenance dict."""
    exp = provenance.get("expert_review")
    if not exp:
        return "_No expert review run._"
    verdict = exp.get("verdict", "?")
    notes = exp.get("reviewer_notes", "")
    means = exp.get("means", {})
    spread = exp.get("spread", {})
    defects = exp.get("defects") or []
    excluded = exp.get("excluded_drafter")
    bits: List[str] = [f"verdict=`{verdict}`"]
    if excluded:
        bits.append(f"drafter `{excluded}` excluded")
    summary = " · ".join(bits)
    rows = [f"  - {axis}: mean {means.get(axis, 0):.2f} "
            f"(spread {spread.get(axis, 0)})"
            for axis in ("correctness", "clarity", "distractor_quality",
                          "difficulty_match", "gre_authenticity")]
    out = f"{summary}\n" + "\n".join(rows)
    if notes:
        out += f"\n  - notes: {notes}"
    if defects:
        out += "\n  - defects: " + "; ".join(defects[:5])
    return out


def _format_verification(q_row: Dict[str, Any]) -> str:
    """A short human-checkable verification line.

    Echoes the marked correct option's letter and text for the reviewer
    so they don't have to scroll up.
    """
    correct = next((o for o in q_row.get("options", []) if o["is_correct"]),
                   None)
    if correct:
        return f"correct=`{correct['label']}` · `{correct['text']}`"
    n = q_row.get("numeric")
    if n and n.get("exact_value") is not None:
        return f"correct=`{n['exact_value']}` (±{n.get('tolerance', 0)})"
    if n and n.get("numerator") is not None:
        return f"correct=`{n['numerator']}/{n['denominator']}`"
    return "_(no marked correct answer)_"


def _embed_svg(asset_path: Path) -> str:
    """Copy and reference an SVG inline in markdown."""
    return f"![figure]({asset_path.name})"


def _embed_png(asset_path: Path) -> str:
    return f"![chart]({asset_path.name})"


def _render_item_block(rec: Dict[str, Any], q_row: Dict[str, Any],
                        assets_relpath: Optional[str] = None) -> str:
    """Render one question's block (stem, options, metadata)."""
    out: List[str] = []
    diff = q_row["difficulty_target"]
    out.append(
        f"#### Q{q_row['qid']} — `{q_row['subtype']}` · "
        f"`{q_row['subtopic']}` · difficulty {diff}/5"
    )
    out.append("")
    if assets_relpath and q_row["subtype"] != "data_interp":
        # geometry SVG (DI charts are shared at the cluster level)
        if assets_relpath.endswith(".svg"):
            out.append(f"![figure]({assets_relpath})")
            out.append("")
    out.append(q_row["stem"])
    out.append("")
    if q_row["options"]:
        out.append(_format_options(q_row["options"]))
        out.append("")
    elif q_row.get("numeric"):
        n = q_row["numeric"]
        if n.get("exact_value") is not None:
            tol = n.get("tolerance", 0)
            out.append(f"**Numeric answer:** {n['exact_value']} "
                       f"(tolerance ±{tol})")
        elif n.get("numerator") is not None:
            out.append(
                f"**Numeric answer (fraction):** "
                f"{n['numerator']}/{n['denominator']}"
            )
        out.append("")
    if q_row["explanation"]:
        out.append("<details><summary>Explanation</summary>\n")
        out.append(q_row["explanation"])
        out.append("\n</details>")
        out.append("")
    # Pipeline metadata
    prov = q_row["provenance"]
    decision = rec.get("decision", "")
    revise = rec.get("revise_rounds", 0)
    out.append(f"_Pipeline: decision=`{decision}`, "
               f"revise_rounds={revise}_")
    out.append("")
    out.append("**Judge axis medians:**")
    out.append("")
    out.append(_format_axes(prov))
    out.append("")
    out.append(f"**Adversarial solvers:** {_format_solver(prov)}")
    out.append("")
    out.append(f"**Verification:** {_format_verification(q_row)}")
    out.append("")
    out.append("**Expert Review:**")
    out.append("")
    out.append(_format_expert_review(prov))
    out.append("")
    return "\n".join(out)


def _resolve_passage_text(recs: List[Dict[str, Any]]) -> str:
    """For an RC cluster, find the passage text on any owner/consumer.

    The owner is the canonical source, but if it failed to persist we
    still try every record's stimulus row in case a consumer carries a
    backup copy.
    """
    from models.database import Question, Stimulus
    candidates = sorted(
        recs,
        key=lambda r: 0 if r.get("cluster_role") == "passage_owner" else 1,
    )
    for r in candidates:
        qid = r.get("qid")
        if not qid:
            continue
        q = Question.get_or_none(Question.id == qid)
        if not q or not q.stimulus_id:
            continue
        s = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
        if not s:
            continue
        text = (s.content or "").strip()
        if text:
            return text
    return ""


def _resolve_di_chart_asset(
    recs: List[Dict[str, Any]],
    assets_src_dir: Path,
) -> Optional[Path]:
    """For a DI cluster, locate the rendered chart asset on disk.

    Looks in three places, in priority order:
    1. The result record's `asset_path` (set by the driver's
       `_attach_di_chart`).
    2. The owner question's `Stimulus.render_spec.asset_path` (the
       persisted variant, robust across re-renders).
    3. The per-run `data/synthetic/runs/<run>/assets/` directory by qid.
    """
    from models.database import Question, Stimulus
    for r in recs:
        ap = r.get("asset_path")
        if ap and Path(ap).exists():
            return Path(ap)
    for r in recs:
        qid = r.get("qid")
        if not qid:
            continue
        q = Question.get_or_none(Question.id == qid)
        if not q or not q.stimulus_id:
            continue
        s = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
        if not s:
            continue
        try:
            spec = json.loads(s.render_spec or "{}")
        except (ValueError, TypeError):
            spec = {}
        ap = spec.get("asset_path")
        if ap:
            cand = Path(ap)
            if cand.exists():
                return cand
            # Try resolving relative to the run's data dir.
            rel = Path("data") / ap if not cand.is_absolute() else cand
            if rel.exists():
                return rel
    # Fallback: scan the assets dir for the owner's item id pattern.
    owner = next((r for r in recs
                  if r.get("cluster_role") == "passage_owner"), None)
    if owner and owner.get("item_id"):
        guess = assets_src_dir / f"{owner['item_id']}.png"
        if guess.exists():
            return guess
    return None


def _render_cluster_block(cluster_id: str,
                           recs: List[Dict[str, Any]],
                           cluster_state: Dict[str, Any],
                           assets_dst: Path,
                           assets_src: Path) -> str:
    """Render an RC or DI cluster — passage/chart shown once, then questions."""
    from models.database import Question
    out: List[str] = []
    rec0 = recs[0]
    if rec0["subtype"].startswith("rc"):
        passage_text = _resolve_passage_text(recs)
        out.append(f"### RC cluster `{cluster_id}` "
                   f"({len(recs)} question{'s' if len(recs)!=1 else ''})")
        out.append("")
        if passage_text:
            out.append("**Passage:**")
            out.append("")
            out.append("> " + passage_text.replace("\n", "\n> "))
            out.append("")
        else:
            out.append("> _(passage missing — owner failed to persist)_")
            out.append("")
    elif rec0["subtype"] == "data_interp":
        out.append(f"### DI cluster `{cluster_id}` "
                   f"({len(recs)} question{'s' if len(recs)!=1 else ''})")
        out.append("")
        chart_path = _resolve_di_chart_asset(recs, assets_src)
        if chart_path is not None:
            dst = assets_dst / chart_path.name
            try:
                shutil.copy2(chart_path, dst)
                out.append("**Chart:**")
                out.append("")
                out.append(f"![chart]({dst.name})")
                out.append("")
            except (OSError, shutil.SameFileError):
                out.append(f"_(chart asset at {chart_path} could not be copied)_")
                out.append("")
        else:
            out.append("> _(chart asset missing — owner failed to persist)_")
            out.append("")

    # Render each question
    for r in recs:
        q = Question.get_or_none(Question.id == r["qid"]) if r["qid"] else None
        if not q:
            continue
        q_row = _q_summary_row(q)
        # For consumers in DI/RC clusters, we don't re-show the stimulus
        q_row["stimulus"] = None
        # Geometry/individual asset stays None for cluster items.
        out.append(_render_item_block(r, q_row, assets_relpath=None))
    return "\n".join(out)


def render_sample_md(
    *,
    run_id: str,
    results: List[Dict[str, Any]],
    cluster_state: Dict[str, Any],
    assets_src_dir: Path,
    out_md: Path,
    roles: Dict[str, Any],
) -> Path:
    """Render the markdown review file. Returns the output path."""
    from models.database import Question, SyntheticGenerationRun

    out_md = Path(out_md)
    assets_dst = out_md.parent / (out_md.stem + "_assets")
    assets_dst.mkdir(parents=True, exist_ok=True)

    persisted = [r for r in results if r["persisted"]]
    drafted = len(results)
    # Split persisted by current DB status so the header reflects the
    # post-expert-review lifecycle. (Question.status is updated by
    # _update_after_expert_review.)
    from models.database import Question
    status_counts: Dict[str, int] = defaultdict(int)
    for r in persisted:
        if not r.get("qid"):
            continue
        q = Question.get_or_none(Question.id == r["qid"])
        status_counts[q.status if q else "missing"] += 1

    # Per-axis aggregation across persisted items
    axis_means: Dict[str, List[float]] = defaultdict(list)
    decisions: Dict[str, int] = defaultdict(int)
    revise_rounds_total = 0
    solver_disagreement_count = 0
    expert_axis_means: Dict[str, List[float]] = defaultdict(list)
    expert_live_count = 0
    expert_draft_count = 0
    expert_run_count = 0
    for r in results:
        decisions[r.get("decision", "unknown")] += 1
        if r.get("medians"):
            for axis, val in r["medians"].items():
                axis_means[axis].append(val)
        revise_rounds_total += r.get("revise_rounds", 0)
        if r.get("decision") == "solver_disagreement":
            solver_disagreement_count += 1
        exp = r.get("expert_review")
        if exp:
            expert_run_count += 1
            if exp.get("verdict") == "live":
                expert_live_count += 1
            else:
                expert_draft_count += 1
            for axis, vals in (exp.get("means") or {}).items():
                expert_axis_means[axis].append(float(vals))

    # Coverage table
    cov_table: Dict[Tuple[str, str], int] = defaultdict(int)
    for r in persisted:
        cov_table[(r["measure"], r["subtopic"])] += 1

    # Header
    md: List[str] = []
    md.append(f"# Synthetic Sample Review — `{run_id}`")
    md.append("")
    md.append(f"_Rendered {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    md.append("")
    md.append("## Run summary")
    md.append("")
    md.append(f"- **Run ID:** `{run_id}`")
    md.append(f"- **Drafted (reached pipeline):** {drafted}")
    md.append(f"- **Persisted (any status):** {len(persisted)} "
              f"({len(persisted)/max(1,drafted)*100:.1f}%)")
    if status_counts:
        breakdown = ", ".join(
            f"`{status}`={count}" for status, count
            in sorted(status_counts.items(), key=lambda kv: -kv[1])
        )
        md.append(f"- **Persisted by lifecycle status:** {breakdown}")
    md.append(f"- **Expert-reviewed:** {expert_run_count} items; "
              f"**promoted live:** {expert_live_count} "
              f"({expert_live_count/max(1,expert_run_count)*100:.1f}% of "
              f"reviewed); **routed to draft:** {expert_draft_count}.")
    md.append(f"- **Total revise rounds across batch:** {revise_rounds_total}")
    md.append(f"- **Solver-disagreement rejections:** "
              f"{solver_disagreement_count}")
    md.append("")
    md.append("### Decision breakdown")
    md.append("")
    md.append("| decision | count |")
    md.append("|---|---|")
    for d, c in sorted(decisions.items(), key=lambda kv: -kv[1]):
        md.append(f"| `{d}` | {c} |")
    md.append("")
    md.append("### Per-axis means (across all judged items)")
    md.append("")
    md.append("| axis | mean median |")
    md.append("|---|---|")
    for axis, vals in axis_means.items():
        if vals:
            md.append(f"| {axis} | {sum(vals)/len(vals):.2f} |")
    md.append("")
    if expert_axis_means:
        md.append("### Expert-review per-axis means")
        md.append("")
        md.append("| axis | mean (across reviewed items) |")
        md.append("|---|---|")
        for axis in ("correctness", "clarity", "distractor_quality",
                      "difficulty_match", "gre_authenticity"):
            vals = expert_axis_means.get(axis) or []
            if vals:
                md.append(f"| {axis} | {sum(vals)/len(vals):.2f} |")
        md.append("")
    md.append("### Model panel")
    md.append("")
    md.append("| role | model |")
    md.append("|---|---|")
    for role, cfg in roles.items():
        md.append(f"| {role} | `{cfg.get('model','?')}` |")
    md.append("")
    md.append("### Coverage (persisted only)")
    md.append("")
    md.append("| measure | subtopic | count |")
    md.append("|---|---|---|")
    for (measure, subtopic), c in sorted(cov_table.items()):
        md.append(f"| {measure} | `{subtopic}` | {c} |")
    md.append("")

    # Body — group by measure, then subtype, then cluster
    md.append("---")
    md.append("")

    # Build cluster map for persisted items.
    by_cluster: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    standalone_quant: List[Dict[str, Any]] = []
    standalone_verbal: List[Dict[str, Any]] = []

    for r in persisted:
        cid = r.get("cluster_id")
        if cid:
            by_cluster[cid].append(r)
        else:
            (standalone_quant if r["measure"] == "quant"
             else standalone_verbal).append(r)

    # Stash the asset_path of the owner into the cluster_state-style dict
    # the renderer needs.
    for cid, recs in by_cluster.items():
        owner = next((r for r in recs
                      if r.get("cluster_role") == "passage_owner"), None)
        if owner:
            for r in recs:
                if r.get("cluster_role") != "passage_owner":
                    r.setdefault("asset_path", owner.get("asset_path"))

    # Render Quant section
    md.append("## Quant")
    md.append("")
    # Standalone quant items (sorted by subtype then subtopic)
    standalone_quant.sort(key=lambda r: (r["subtype"], r["subtopic"]))
    for r in standalone_quant:
        q = Question.get_or_none(Question.id == r["qid"])
        if not q:
            continue
        q_row = _q_summary_row(q)
        # Geometry asset?
        asset_rel = None
        if r.get("asset_path"):
            src = Path(r["asset_path"])
            if src.exists():
                dst = assets_dst / src.name
                shutil.copy2(src, dst)
                asset_rel = dst.name
        md.append(_render_item_block(r, q_row, assets_relpath=asset_rel))
        md.append("---")
        md.append("")

    # Quant clusters (DI)
    quant_cluster_ids = sorted({cid for cid, recs in by_cluster.items()
                                  if recs and recs[0]["measure"] == "quant"})
    for cid in quant_cluster_ids:
        recs = sorted(by_cluster[cid],
                      key=lambda r: (r["cluster_role"] != "passage_owner",
                                      r["difficulty_target"]))
        md.append(_render_cluster_block(cid, recs, cluster_state,
                                          assets_dst, assets_src_dir))
        md.append("---")
        md.append("")

    # Render Verbal section
    md.append("## Verbal")
    md.append("")
    standalone_verbal.sort(key=lambda r: (r["subtype"], r["subtopic"]))
    for r in standalone_verbal:
        q = Question.get_or_none(Question.id == r["qid"])
        if not q:
            continue
        q_row = _q_summary_row(q)
        md.append(_render_item_block(r, q_row, assets_relpath=None))
        md.append("---")
        md.append("")
    # Verbal clusters (RC)
    verbal_cluster_ids = sorted({cid for cid, recs in by_cluster.items()
                                   if recs and recs[0]["measure"] == "verbal"})
    for cid in verbal_cluster_ids:
        recs = sorted(by_cluster[cid],
                      key=lambda r: (r["cluster_role"] != "passage_owner",
                                      r["difficulty_target"]))
        md.append(_render_cluster_block(cid, recs, cluster_state,
                                          assets_dst, assets_src_dir))
        md.append("---")
        md.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(md), encoding="utf-8")
    return out_md


def main():
    """Standalone re-render mode: rebuild the markdown from the DB."""
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--assets-src", required=False, default="")
    args = p.parse_args()

    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    from models.database import init_db, Question
    init_db()

    rows = list(Question.select().where(Question.run_id == args.run_id))
    results = []
    for q in rows:
        results.append({
            "item_id": str(q.id),
            "qid": q.id,
            "persisted": True,
            "decision": "rerender",
            "subtype": q.subtype,
            "subtopic": q.subtopic,
            "topic": q.topic,
            "measure": q.measure,
            "difficulty_target": q.difficulty_target,
            "cluster_role": None,
            "cluster_id": None,
        })
    render_sample_md(
        run_id=args.run_id,
        results=results,
        cluster_state={},
        assets_src_dir=Path(args.assets_src) if args.assets_src else Path("."),
        out_md=Path(args.out),
        roles={},
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
