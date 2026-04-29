"""Inline broken `src='images/...'` references into self-contained data: URIs.

Some stimuli were shipped with relative image paths that the wxPython WebView
can't resolve (only ~2 Kaplan items, but each one broke a 5-question DI
cluster). This script scans every Stimulus row, finds any remaining relative
image paths, resolves them against the known asset roots, and rewrites the
HTML with base64-encoded `data:` URIs.

Idempotent: already-inlined `data:` URIs are skipped; runs with zero writes
if the DB is already clean.

Usage:
    venv/bin/python scripts/inline_image_srcs.py --db data/gre_user.db
    venv/bin/python scripts/inline_image_srcs.py --db data/gre_mock.db
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sqlite3
from pathlib import Path

# Checked in order; first existing file wins.
IMAGE_ROOTS = [
    Path("data/extracted/kaplan/images"),
    Path("data/extracted/kaplan/assets"),
    Path("data/extracted/princeton/images"),
    Path("data/extracted/manhattan/ch7_figs"),
]


def _find_asset(rel: str, base: Path) -> Path | None:
    name = Path(rel).name
    for root in IMAGE_ROOTS:
        candidate = (base / root / name).resolve()
        if candidate.exists():
            return candidate
    return None


def _inline_content(content: str, base: Path) -> str:
    pattern = re.compile(r"""src=["']([^"'>]+)["']""")

    def repl(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith("data:") or src.startswith("http"):
            return m.group(0)
        asset = _find_asset(src, base)
        if asset is None:
            return m.group(0)
        mime = mimetypes.guess_type(str(asset))[0] or "image/jpeg"
        b64 = base64.b64encode(asset.read_bytes()).decode("ascii")
        return m.group(0).replace(src, f"data:{mime};base64,{b64}")

    return pattern.sub(repl, content)


def run(db_path: Path, repo_root: Path) -> tuple[int, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, content FROM stimulus "
            "WHERE content LIKE '%src=\"images/%' OR content LIKE \"%src='images/%\""
        )
        rows = cur.fetchall()
        updated = 0
        skipped = 0
        for sid, content in rows:
            new_content = _inline_content(content, repo_root)
            if new_content == content:
                skipped += 1
                continue
            cur.execute("UPDATE stimulus SET content = ? WHERE id = ?", (new_content, sid))
            updated += 1
        conn.commit()
        return updated, skipped
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="path to the sqlite DB")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repo root for resolving image asset paths",
    )
    args = parser.parse_args()

    updated, skipped = run(args.db.resolve(), args.repo_root.resolve())
    print(f"{args.db}: {updated} updated, {skipped} unchanged (asset not found)")


if __name__ == "__main__":
    main()
