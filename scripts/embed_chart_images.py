"""
Migrate Stimulus rows that reference chart images via file:// URLs to use
inline base64 data URIs. Fixes the wxPython WebView issue where file:// images
don't render reliably from inline-loaded HTML pages.
"""
import base64
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, init_db, Stimulus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "data" / "images"


def to_data_uri(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


FILE_SRC_RE = re.compile(r'src="file://([^"]+)"')


def main():
    init_db()
    db.connect(reuse_if_open=True)

    rows = list(Stimulus.select().where(Stimulus.content.contains("file://")))
    print(f"Stimuli with file:// references: {len(rows)}")

    fixed, missing = 0, 0
    for s in rows:
        new_content = s.content
        for m in FILE_SRC_RE.finditer(s.content):
            file_path = m.group(1)
            data_uri = to_data_uri(file_path)
            if not data_uri:
                missing += 1
                print(f"  Stim {s.id}: missing image at {file_path}")
                continue
            new_content = new_content.replace(f'file://{file_path}', data_uri)
        if new_content != s.content:
            s.content = new_content
            s.save()
            fixed += 1

    print(f"Fixed {fixed} stimuli, {missing} missing image files")


if __name__ == "__main__":
    main()
