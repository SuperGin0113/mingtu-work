"""
V2 chunking and embedding pipeline for Milvus.

This version writes only real child chunks. It does not write parent rows and
does not store doc_id, parent_id, or level fields in Milvus.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import time
import traceback

import ijson
from dotenv import load_dotenv

load_dotenv()

from chunk_pipeline import (
    active_embed_name,
    get_embed,
    get_splitter,
    log,
    split_parents,
    tok_len,
)
from milvus_db import DEFAULT_COLLECTION, get_client, init_collection

DEFAULT_JSON = os.environ.get("DOC_JSON", "doc_items_202604231612.json")
CONTENT_MAX = 65_535


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _body_start(md: str, section: dict) -> int:
    body = section["body"]
    pos = md.find(body, section["start"])
    if pos >= 0:
        return pos

    stripped = body.strip()
    if stripped:
        pos = md.find(stripped, section["start"])
        if pos >= 0:
            return pos

    return int(section["start"])


def _child_spans(body: str, body_start: int, children: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for child in children:
        target = child
        pos = body.find(target, cursor)

        if pos < 0:
            stripped = child.strip()
            if stripped:
                stripped_pos = body.find(stripped, cursor)
                if stripped_pos >= 0:
                    target = stripped
                    pos = stripped_pos

        if pos < 0:
            pos = body.find(child)
            target = child

        if pos < 0:
            pos = cursor
            target = child

        start = body_start + pos
        end = start + len(target)
        spans.append((start, end))
        cursor = min(pos + 1, len(body))
    return spans


def build_rows(item: dict) -> tuple[list[dict], str]:
    source_doc_id = int(item["id"])
    doc_guid = item.get("doc_guid") or ""
    md = item.get("doc_md_clean") or ""
    if not md:
        return [], "empty_md"

    sections = split_parents(md)
    if not sections:
        return [], "no_sections"

    splitter = get_splitter()
    plan = []
    for section in sections:
        children = [c for c in splitter.split_text(section["body"]) if c.strip()]
        body_start = _body_start(md, section)
        plan.append(
            {
                "heading": section["heading"],
                "children": children,
                "spans": _child_spans(section["body"], body_start, children),
            }
        )

    all_children = [child for section in plan for child in section["children"]]
    if not all_children:
        return [], "no_chunks"

    log(
        f"source_doc_id={source_doc_id} sections={len(plan)} "
        f"chunks={len(all_children)} -> embedding ..."
    )
    t0 = time.time()
    embeddings = get_embed().get_text_embedding_batch(all_children, show_progress=False)
    log(f"source_doc_id={source_doc_id} embedding done ({time.time() - t0:.1f}s)")

    now = _utc_now()
    meta_common = {
        "doc_guid": _truncate(doc_guid, 64),
        "title": _truncate(item.get("title"), 1024),
        "court_line": _truncate(item.get("court_line"), 512),
        "title_description": _truncate(item.get("title_description"), 2048),
        "create_time": now,
        "update_time": now,
    }

    rows: list[dict] = []
    chunk_order = 0
    embedding_index = 0
    for section in plan:
        heading = _truncate(section["heading"], 512)
        for content, (char_start, char_end) in zip(section["children"], section["spans"]):
            rows.append(
                {
                    "chunk_order": chunk_order,
                    "heading": heading,
                    "content": _truncate(content, CONTENT_MAX),
                    "token_count": int(tok_len(content)),
                    "char_start": int(char_start),
                    "char_end": int(char_end),
                    "embed_model": active_embed_name(),
                    "embedding": embeddings[embedding_index],
                    **meta_common,
                }
            )
            chunk_order += 1
            embedding_index += 1

    return rows, f"ok({len(rows)} rows)"


def iter_doc_items(json_path: str):
    with open(json_path, "rb") as f:
        yield from ijson.items(f, "doc_items.item")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=DEFAULT_JSON, help="Path to doc_items JSON.")
    parser.add_argument("--doc-id", type=int, help="Only process the given source document id.")
    parser.add_argument("--batch", action="store_true", help="Process all items in the JSON.")
    parser.add_argument("--limit", type=int, help="Limit processed JSON items.")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    args = parser.parse_args()

    if args.doc_id is None and not args.batch:
        parser.error("Specify --doc-id or --batch.")

    log(f"embed model: {active_embed_name()}")
    log(f"source: {args.json} -> Milvus collection {args.collection}")

    init_collection(args.collection)
    client = get_client()

    ok = skip = fail = 0
    visited = 0
    for item in iter_doc_items(args.json):
        source_doc_id = int(item["id"])
        if args.doc_id is not None and source_doc_id != args.doc_id:
            continue

        visited += 1
        try:
            rows, status = build_rows(item)
            if rows:
                client.insert(collection_name=args.collection, data=rows)
                ok += 1
                log(f"[done] source_doc_id={source_doc_id} {status}")
            else:
                skip += 1
                log(f"[skip] source_doc_id={source_doc_id} {status}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            traceback.print_exc()
            log(f"[fail] source_doc_id={source_doc_id} {type(exc).__name__}: {exc}")

        if args.doc_id is not None:
            break
        if args.limit and visited >= args.limit:
            break

    log(f"summary: ok={ok} skip={skip} fail={fail} visited={visited}")
    try:
        client.flush(collection_name=args.collection)
    except Exception:
        pass


if __name__ == "__main__":
    main()
