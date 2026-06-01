#!/usr/bin/env python3
"""
GRE Mock Database Audit -- Figure Renderability
===============================================

The existing ``audit_image_gaps.py`` keys off the empty ``figure_refs``
column plus a geometry/spatial-language regex, which over-counts ~30x:
nearly every item has empty ``figure_refs`` because real figures are
base64-inlined into ``stimulus.content`` instead. This audit measures what
the user actually sees by replicating the renderer's resolution logic
(``screens/question_screen.py`` renders ``stimulus.content`` only) and
classifying every figure-bearing item by whether a figure will appear.

Classifications:
  RENDERS              -- content carries an inline <img> or <table>; shows.
  PHANTOM_SPEC_ASSET   -- caption-only content; render_spec.asset_path points
                          at a file that does NOT resolve under data/images/.
                          (Never renders -- asset missing AND renderer ignores
                          render_spec anyway.)
  PHANTOM_SPEC_WIRED   -- caption-only content; render_spec present and asset
                          resolves OR carries a reconstructable geometry spec,
                          but the renderer never reads render_spec -> still
                          blank until WS-B wires it. Recoverable.
  PHANTOM_CAPTION_ONLY -- caption-only content, no usable render_spec. Either
                          solvable from text or must be retired.
  PROMPT_FIGURE_NONE   -- prompt references a figure ("figure above/below",
                          "shown", "not drawn to scale") but there is no
                          stimulus and no figure at all.

Usage:
    python scripts/audit_figure_render.py [--summary] [--export]
    python scripts/audit_figure_render.py --db data/gre_user.db --live-only

Exit codes: 0 = no phantom figures, 1 = phantom figures found, 2 = error.

The exported manifest (data/audits/figure_render_manifest_<date>.json) is the
canonical worklist consumed by WS-B.
"""

import sys
import os
import re
import json
import argparse
import datetime
from collections import defaultdict

import sqlite3

IMAGES_DIR = "data/images"

# RC passages are sometimes mis-typed as 'graph' stimuli (Kaplan import) but
# carry the full passage text and need no figure. They render fine; the wrong
# stimulus_type label is a taxonomy cleanup, not a missing figure, so they are
# excluded from figure-bearing classification here.
RC_SUBTYPES = {"rc_single", "rc_multi", "rc_select_passage"}

PROMPT_FIGURE_RE = re.compile(
    r"figure (?:above|below|shown)|shown (?:above|below)|"
    r"in the figure|the figure (?:above|below)|not drawn to scale",
    re.IGNORECASE,
)


def _content_renders(content):
    c = content or ""
    return ("<img" in c.lower()) or ("<table" in c.lower())


def _resolve_asset(asset_path):
    """Mirror the renderer's only readable base path (data/images/). The
    render_spec asset_path is stored as e.g. 'assets/phase1-...svg'; the
    renderer can only load files that live under data/images/."""
    if not asset_path:
        return False
    base = os.path.basename(asset_path)
    for cand in (os.path.join(IMAGES_DIR, asset_path),
                 os.path.join(IMAGES_DIR, base)):
        if os.path.exists(cand):
            return True
    return False


def classify(row):
    (qid, source, subtype, status, prompt,
     stim_id, stim_type, content, render_spec) = row

    has_stim = stim_id is not None
    figure_bearing = (has_stim and (stim_type in ("graph", "table"))
                      and subtype not in RC_SUBTYPES)
    prompt_refs_figure = (bool(PROMPT_FIGURE_RE.search(prompt or ""))
                          and subtype not in RC_SUBTYPES)

    if not figure_bearing and not prompt_refs_figure:
        return None  # not a figure item

    if figure_bearing and _content_renders(content):
        return ("RENDERS", None)

    if not figure_bearing and prompt_refs_figure:
        # prompt promises a figure but there's no figure-bearing stimulus
        if has_stim and _content_renders(content):
            return ("RENDERS", None)
        return ("PROMPT_FIGURE_NONE", None)

    # figure_bearing but content does not render -> inspect render_spec
    spec = None
    if render_spec:
        try:
            spec = json.loads(render_spec)
        except Exception:
            spec = None

    if spec:
        asset = spec.get("asset_path")
        has_geometry = bool(spec.get("spec")) or spec.get("kind") == "svg_geometry"
        if _resolve_asset(asset):
            return ("PHANTOM_SPEC_WIRED", dict(asset=asset, resolvable=True))
        if has_geometry:
            # reconstructable from spec even though asset is missing
            return ("PHANTOM_SPEC_WIRED", dict(asset=asset, resolvable=False,
                                               reconstructable=True,
                                               geometry_kind=spec.get("geometry_kind")))
        return ("PHANTOM_SPEC_ASSET", dict(asset=asset))

    return ("PHANTOM_CAPTION_ONLY", None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/gre_mock.db")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    where = "WHERE 1=1"
    if args.live_only:
        where += " AND q.status='live'"
    rows = conn.execute(
        f"""
        SELECT q.id, q.source, q.subtype, q.status, q.prompt,
               s.id, s.stimulus_type, s.content, s.render_spec
        FROM question q
        LEFT JOIN stimulus s ON q.stimulus_id = s.id
        {where}
        """
    ).fetchall()
    conn.close()

    buckets = defaultdict(list)
    for row in rows:
        res = classify(row)
        if res is None:
            continue
        kind, detail = res
        buckets[kind].append(dict(
            qid=row[0], source=row[1], subtype=row[2], status=row[3],
            detail=detail,
        ))

    phantom_kinds = ("PHANTOM_SPEC_ASSET", "PHANTOM_SPEC_WIRED",
                     "PHANTOM_CAPTION_ONLY", "PROMPT_FIGURE_NONE")
    n_phantom = sum(len(buckets[k]) for k in phantom_kinds)

    print(f"Figure-render audit on {args.db} (live_only={args.live_only})")
    for kind in ("RENDERS",) + phantom_kinds:
        items = buckets.get(kind, [])
        by_src = defaultdict(int)
        for it in items:
            by_src[it["source"]] += 1
        print(f"  {kind:22s} {len(items):4d}  {dict(by_src)}")
    print(f"  --> total phantom (user-visible breakage): {n_phantom}")

    if not args.summary:
        for kind in phantom_kinds:
            for it in buckets.get(kind, []):
                print(f"  - q{it['qid']} [{it['source']}/{it['subtype']}/{it['status']}] "
                      f"{kind} {it['detail'] or ''}")

    if args.export:
        os.makedirs("data/audits", exist_ok=True)
        stamp = datetime.date.today().isoformat()
        path = f"data/audits/figure_render_manifest_{stamp}.json"
        with open(path, "w") as f:
            json.dump(dict(
                generated=datetime.datetime.now().isoformat(timespec="seconds"),
                db=args.db, live_only=args.live_only,
                counts={k: len(v) for k, v in buckets.items()},
                phantom_total=n_phantom,
                buckets={k: buckets.get(k, []) for k in phantom_kinds},
            ), f, indent=2)
        print(f"  -> manifest written: {path}")

    return 1 if n_phantom else 0


if __name__ == "__main__":
    sys.exit(main())
