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
import os
import re
import sys
import time
import traceback
from pathlib import Path


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# stdout 无缓冲（以防被重定向到文件时 Python 默认行/块缓冲把实时输出吞掉）
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# HF 镜像（在 import llama_index / transformers 之前设置才生效）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import psycopg2
from psycopg2.extras import execute_values

from db import DB_CONFIG, DB_NAME

CHUNK_TOKENS = 512
CHUNK_OVERLAP = 50
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "20"))
HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)
HF_HUB_DIR = Path.home() / ".cache" / "huggingface" / "hub"

# OpenAI 兼容 /v1/embeddings 服务
EMBED_URL = os.environ.get("EMBED_URL")
EMBED_MODEL = os.environ.get("EMBED_MODEL","bge-m3")
EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT", "60"))
# splitter 需要一个本地 tokenizer 做 token 计数
TOKENIZER_MODEL = os.environ.get("TOKENIZER_MODEL", "BAAI/bge-m3")


def active_embed_name() -> str:
    return EMBED_MODEL


class UpstreamServiceError(RuntimeError):
    """Raised when an external embedding/rerank service request fails."""


# ── 切分（父块） ──────────────────────────────────────────────────
def split_parents(md: str) -> list[dict]:
    md = md or ""
    ms = list(HEADING_RE.finditer(md))
    if not ms:
        body = md.strip()
        return [{"heading": "", "body": body, "start": 0, "end": len(md)}] if body else []
    out: list[dict] = []
    preamble = md[: ms[0].start()].strip()
    for i, m in enumerate(ms):
        start = m.start()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        body = md[start:end].rstrip()
        if i == 0 and preamble:
            body = preamble + "\n\n" + body
            start = 0
        out.append({"heading": m.group(1).strip(), "body": body, "start": start, "end": end})
    return out


# ── LlamaIndex 单例 ──────────────────────────────────────────────
_tokenizer = None
_embed = None
_splitter = None


def resolve_local_model_path(model_name: str) -> str | None:
    parts = model_name.split("/", 1)
    if len(parts) != 2:
        return None

    model_dir = HF_HUB_DIR / f"models--{parts[0]}--{parts[1]}"
    refs_main = model_dir / "refs" / "main"
    if refs_main.exists():
        ref = refs_main.read_text(encoding="utf-8").strip()
        snap = model_dir / "snapshots" / ref
        if snap.exists() and any(snap.iterdir()):
            return str(snap)

    snapshots_dir = model_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    candidates = [p for p in snapshots_dir.iterdir() if p.is_dir() and any(p.iterdir())]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(latest)


def _resolve_tokenizer(name: str) -> tuple[str, dict]:
    local = resolve_local_model_path(name)
    if local:
        return local, {"local_files_only": True}
    return name, {}


def get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer

        src, kw = _resolve_tokenizer(TOKENIZER_MODEL)
        _tokenizer = AutoTokenizer.from_pretrained(src, **kw)
    return _tokenizer


def _build_http_embed():
    import requests
    from llama_index.core.embeddings import BaseEmbedding
    from pydantic import Field

    class HTTPEmbedding(BaseEmbedding):
        """OpenAI 兼容 /v1/embeddings 的 LlamaIndex 适配器。"""

        url: str = Field(default=EMBED_URL)
        timeout: float = Field(default=EMBED_TIMEOUT)

        def _call(self, texts: list[str]) -> list[list[float]]:
            if not self.url:
                raise UpstreamServiceError("embedding upstream failed: EMBED_URL is not configured")
            try:
                resp = requests.post(
                    self.url,
                    json={"input": texts, "model": self.model_name},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                body = exc.response.text[:500] if exc.response is not None else ""
                raise UpstreamServiceError(
                    f"embedding upstream failed: url={self.url} status={status} body={body}"
                ) from exc
            except requests.RequestException as exc:
                raise UpstreamServiceError(
                    f"embedding upstream failed: url={self.url} error={exc}"
                ) from exc
            data = resp.json()["data"]
            data.sort(key=lambda x: x["index"])
            return [d["embedding"] for d in data]

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._call([query])[0]

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._call([text])[0]

        def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for i in range(0, len(texts), self.embed_batch_size):
                out.extend(self._call(texts[i : i + self.embed_batch_size]))
            return out

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._get_query_embedding(query)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._get_text_embedding(text)

    log(f"using HTTP embed service: {EMBED_URL} (model={EMBED_MODEL}, batch={EMBED_BATCH_SIZE})")
    return HTTPEmbedding(
        model_name=EMBED_MODEL,
        embed_batch_size=EMBED_BATCH_SIZE,
        url=EMBED_URL,
        timeout=EMBED_TIMEOUT,
    )


def get_embed():
    global _embed
    if _embed is None:
        _embed = _build_http_embed()
    return _embed


def get_splitter():
    global _splitter
    if _splitter is None:
        from llama_index.core.node_parser import SentenceSplitter

        _splitter = SentenceSplitter(
            chunk_size=CHUNK_TOKENS,
            chunk_overlap=CHUNK_OVERLAP,
            tokenizer=get_tokenizer().encode,
        )
    return _splitter


def tok_len(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False))


# ── DB ────────────────────────────────────────────────────────────
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
    global EMBED_MODEL, _embed

    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="处理单条 doc_items.id")
    ap.add_argument("--batch", action="store_true", help="批量处理")
    ap.add_argument("--force", action="store_true", help="批量时忽略状态，全量重跑")
    ap.add_argument("--table", default="chunks_test", choices=["chunks", "chunks_test"])
    ap.add_argument("--limit", type=int, help="批量 LIMIT (按 md 长度升序)")
    ap.add_argument(
        "--model-name",
        default=None,
        help=f"Embedding model 名称，作为 HTTP 服务的 model 字段（默认 {EMBED_MODEL}）。",
    )
    args = ap.parse_args()

    if args.model_name:
        if args.model_name != EMBED_MODEL:
            EMBED_MODEL = args.model_name
            _embed = None

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
