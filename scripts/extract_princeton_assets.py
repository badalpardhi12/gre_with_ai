"""Extract image assets from the Princeton Review EPUB.

The EPUB is a ZIP archive; images live under OEBPS/images/. The consolidator
JSON at data/extracted/princeton/princeton_extracted.json references images as
e.g. "images/Revi_9780307945396_fi###_r1.gif" — the references are basename
scoped under an "images/" prefix. This script writes the extracted files to
data/extracted/princeton/images/ so those references resolve.

Idempotent: files that already exist on disk are skipped.

Notes:
    Ebooks and extraction artifacts are gitignored; this worktree does not have
    local copies. We read from the canonical main-repo paths (absolute) and
    write into the worktree's data/extracted/princeton/images/.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
MAIN_REPO_ROOT = Path("/Users/chiku/Documents/side_projects/gre_with_ai")
EPUB_DEFAULT = (
    MAIN_REPO_ROOT
    / "data"
    / "ebooks"
    / "Princeton Review - 1,014 GRE Practice Questions, 3rd Edition-Princeton Review (2012).epub"
)
OUT_DEFAULT = WORKTREE_ROOT / "data" / "extracted" / "princeton" / "images"

IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".svg"}


def extract(epub_path: Path, out_dir: Path):
    if not epub_path.exists():
        raise FileNotFoundError(f"EPUB not found: {epub_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    skipped = 0
    total_images = 0
    with zipfile.ZipFile(epub_path) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if not any(lower.endswith(ext) for ext in IMAGE_EXTS):
                continue
            total_images += 1
            basename = Path(name).name
            dst = out_dir / basename
            if dst.exists():
                skipped += 1
                continue
            with zf.open(name) as src, open(dst, "wb") as out:
                out.write(src.read())
            extracted += 1
    return total_images, extracted, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epub", default=str(EPUB_DEFAULT))
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    args = parser.parse_args()
    total, wrote, skipped = extract(Path(args.epub), Path(args.out))
    print(f"EPUB images: {total}")
    print(f"Newly extracted: {wrote}")
    print(f"Skipped (already on disk): {skipped}")
    print(f"Output dir: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
