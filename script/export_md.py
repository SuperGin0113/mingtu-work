"""一次性导出：doc_items.doc_md_clean → data/doc_md_clean/<id>_<title>.md"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from script.db import DB_CONFIG, DB_NAME
from script.project_paths import DATA_DIR

OUT_DIR = DATA_DIR / "doc_md_clean"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str | None) -> str:
    return _INVALID.sub("_", (text or "").strip()) or "untitled"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, doc_md_clean
                FROM doc_items
                WHERE doc_md_clean IS NOT NULL
                ORDER BY id
                """
            )
            count = 0
            for row_id, title, md in cur.fetchall():
                name = f"{row_id:04d}_{safe_name(title)}.md"
                (OUT_DIR / name).write_text(md, encoding="utf-8")
                count += 1
    finally:
        conn.close()

    print(f"Exported {count} files to {OUT_DIR}")


if __name__ == "__main__":
    main()
