"""
Westlaw 文档父子分块 + BGE-M3 向量化管线（LlamaIndex 版）。

切分：
  父块 —— 按 md 的二级标题 `## ` 切段（自写正则，首个 `##` 之前的导语并入第一个父块）
  子块 —— LlamaIndex `SentenceSplitter(chunk_size=512, chunk_overlap=50)`，
          以 BGE-M3 tokenizer 做长度计数

Embedding：
  LlamaIndex `HuggingFaceEmbedding("BAAI/bge-m3")`，批大小 16，dense 1024 维

存储：
  psycopg2 写入 `chunks` / `chunks_test` 单表，embedding 目前存为 REAL[]，
  pgvector 就位后可一条 ALTER 切回 vector(1024)。

CLI:
    python chunk_pipeline.py --id 42 --table chunks_test
    python chunk_pipeline.py --batch --table chunks_test --limit 5 --force
"""

from __future__ import annotations

import argparse
import time
import traceback

import psycopg2
from psycopg2.extras import execute_values

from script.db import DB_CONFIG, DB_NAME
from utils import embedding as _embedding
from utils.chunking import TOKENIZER_MODEL, get_splitter, split_parents, tok_len
from utils.embedding import active_embed_name, get_embed
from utils.logging import log


def mark_status(cur, doc_id: int, status: int, reason: str | None, count: int | None):
    cur.execute(
        "UPDATE doc_items SET chunk_process_status=%s, chunk_process_fail_reason=%s, "
        "chunk_count=%s WHERE id=%s",
        (status, reason, count, doc_id),
    )


def process_doc(conn, doc_id: int, table: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_md_clean FROM doc_items WHERE id=%s AND doc_md_clean IS NOT NULL",
            (doc_id,),
        )
        row = cur.fetchone()
        if not row:
            log(f"[skip] doc_id={doc_id} 无 doc_md_clean")
            return
        md = row[0]
        mark_status(cur, doc_id, 1, None, None)
        conn.commit()

    try:
        log(f"doc_id={doc_id} len(md)={len(md)} splitting ...")
        parents = split_parents(md)
        if not parents:
            with conn.cursor() as cur:
                mark_status(cur, doc_id, 2, "empty_md", 0)
                conn.commit()
            log(f"[done] doc_id={doc_id} 空 md")
            return

        splitter = get_splitter()
        plan = []
        for order, p in enumerate(parents):
            children = [c for c in splitter.split_text(p["body"]) if c.strip()]
            plan.append(
                {
                    "heading": p["heading"],
                    "body": p["body"],
                    "start": p["start"],
                    "end": p["end"],
                    "order": order,
                    "p_tok": tok_len(p["body"]),
                    "children": children,
                }
            )

        all_children = [c for item in plan for c in item["children"]]
        log(f"doc_id={doc_id} parents={len(plan)} children={len(all_children)} → embedding ...")
        t0 = time.time()
        embeddings = get_embed().get_text_embedding_batch(all_children, show_progress=True)
        log(f"doc_id={doc_id} embedding done ({time.time() - t0:.1f}s)")

        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE doc_id=%s", (doc_id,))

            total, ei = 0, 0
            doc_chunk_order = 0
            for item in plan:
                parent_chunk_order = doc_chunk_order
                doc_chunk_order += 1
                cur.execute(
                    f"""
                    INSERT INTO {table} (doc_id, parent_id, level, heading,
                                         chunk_order, content, token_count, char_start, char_end)
                    VALUES (%s, NULL, 0, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (doc_id, item["heading"], parent_chunk_order, item["body"],
                     item["p_tok"], item["start"], item["end"]),
                )
                parent_id = cur.fetchone()[0]

                rows = []
                for ctext in item["children"]:
                    rows.append((
                        doc_id, parent_id, item["heading"], doc_chunk_order,
                        ctext, tok_len(ctext), embeddings[ei], active_embed_name(),
                    ))
                    doc_chunk_order += 1
                    ei += 1
                if rows:
                    execute_values(
                        cur,
                        f"""
                        INSERT INTO {table} (doc_id, parent_id, level, heading,
                                             chunk_order, content, token_count, embedding, embed_model)
                        VALUES %s
                        """,
                        rows,
                        template="(%s, %s, 1, %s, %s, %s, %s, %s, %s)",
                    )
                total += 1 + len(rows)

            mark_status(cur, doc_id, 2, None, total)
            conn.commit()
        log(f"[done] doc_id={doc_id} parents={len(plan)} children={len(all_children)}")

    except Exception as e:
        conn.rollback()
        reason = f"{type(e).__name__}: {e}"[:1000]
        traceback.print_exc()
        with conn.cursor() as cur:
            mark_status(cur, doc_id, 3, reason, None)
            conn.commit()
        log(f"[fail] doc_id={doc_id} {reason}")


def pick_ids(cur, force: bool, limit: int | None) -> list[int]:
    sql = "SELECT id FROM doc_items WHERE doc_md_clean IS NOT NULL "
    if not force:
        sql += "AND COALESCE(chunk_process_status,0) IN (0,3) "
    sql += "ORDER BY LENGTH(doc_md_clean) ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="处理单条 doc_items.id")
    ap.add_argument("--batch", action="store_true", help="批量处理")
    ap.add_argument("--force", action="store_true", help="批量时忽略状态，全量重跑")
    ap.add_argument("--table", default="chunks_test", choices=["chunks", "chunks_test"])
    ap.add_argument("--limit", type=int, help="批量 LIMIT (按 md 长度升序)")
    ap.add_argument(
        "--model-name",
        default=None,
        help=f"Embedding model 名称，作为 HTTP 服务的 model 字段（默认 {_embedding.EMBED_MODEL}）。",
    )
    args = ap.parse_args()

    if args.model_name:
        if args.model_name != _embedding.EMBED_MODEL:
            _embedding.EMBED_MODEL = args.model_name
            _embedding._embed = None

    log(f"embed model: {active_embed_name()}, tokenizer: {TOKENIZER_MODEL}")

    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    try:
        if args.id is not None:
            process_doc(conn, args.id, args.table)
        elif args.batch:
            with conn.cursor() as cur:
                ids = pick_ids(cur, args.force, args.limit)
            log(f"待处理 {len(ids)} 条 → {args.table}: {ids}")
            for did in ids:
                process_doc(conn, did, args.table)
        else:
            ap.error("必须指定 --id 或 --batch")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
