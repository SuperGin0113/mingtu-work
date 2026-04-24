"""
检索管线(Milvus 原生向量 + Milvus 原生 BM25 + 客户端 min-max 加权融合)。

由 router 的 `/retrieve` endpoint 调用。

主要能力:
  - cos_weight=1.0 → 纯向量检索(Milvus HNSW/COSINE)
  - cos_weight=0.0 → 纯 BM25 检索(Milvus SPARSE_INVERTED_INDEX/BM25)
  - 0 < cos_weight < 1 → 两路单独搜索 + per-query min-max 归一 + 加权融合
  - 可选重排(本地 /v1/rerank 服务;环境变量 RERANK_URL / RERANK_MODEL)

数据来源:Milvus 集合(schema 见 milvus_db.build_schema,sparse 由 BM25 Function 从 content 自动生成)。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field
from pymilvus import Collection

from milvus_db import ALIAS, DEFAULT_COLLECTION, connect as milvus_connect
from utils.embedding import active_embed_name, get_embed
from utils.errors import UpstreamServiceError
from utils.logging import log

CANDIDATE_MULTIPLIER = 1
DEFAULT_TOP_K = 30
RERANK_URL = os.environ.get("RERANK_URL")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3")
RERANK_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "60"))
RERANK_SCORE_TYPE = "raw"

OUTPUT_FIELDS = [
    "id",
    "doc_guid",
    "heading",
    "content",
    "title",
    "court_line",
    "title_description",
]

DENSE_SEARCH_PARAM = {"metric_type": "COSINE", "params": {"ef": 64}}
SPARSE_SEARCH_PARAM = {"metric_type": "BM25"}


# ── 配置模型 ──────────────────────────────────────────────────────
class RetrievalConfig(BaseModel):
    cos_weight: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="向量分权重:1.0=纯向量,0.0=纯 BM25,其它值 → 与 BM25 加权融合(alpha=cos_weight)。",
    )
    top_k: int = Field(
        DEFAULT_TOP_K,
        ge=1,
        le=100,
        description="返回结果条数;默认 30。",
    )
    rerank_enabled: bool = False


# ── 融合算法(per-query min-max + 加权) ──────────────────────────
def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _weighted_sum_fuse(
    cos_list: list[tuple[int, float]],
    bm25_list: list[tuple[int, float]],
    alpha: float,
    top: int,
) -> list[tuple[int, float]]:
    cos_ids = [x[0] for x in cos_list]
    cos_norm = dict(zip(cos_ids, _minmax([x[1] for x in cos_list])))
    bm25_ids = [x[0] for x in bm25_list]
    bm25_norm = dict(zip(bm25_ids, _minmax([x[1] for x in bm25_list])))
    fused = [
        (nid, alpha * cos_norm.get(nid, 0.0) + (1 - alpha) * bm25_norm.get(nid, 0.0))
        for nid in set(cos_norm) | set(bm25_norm)
    ]
    return sorted(fused, key=lambda x: -x[1])[:top]


# ── 重排(本地 /v1/rerank;分数为 raw logits) ────────
def _rerank(query: str, hits: list[dict], model_name: str, top_k: int) -> list[dict]:
    import requests

    if not RERANK_URL:
        raise UpstreamServiceError("rerank upstream failed: RERANK_URL is not configured")

    docs = [h["content"][:1200] for h in hits]
    t0 = time.time()
    try:
        resp = requests.post(
            RERANK_URL,
            json={
                "model": model_name,
                "query": query,
                "documents": docs,
                "top_n": len(docs),
                "return_documents": False,
                "score_type": RERANK_SCORE_TYPE,
            },
            timeout=RERANK_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:500] if exc.response is not None else ""
        raise UpstreamServiceError(
            f"rerank upstream failed: url={RERANK_URL} status={status} body={body}"
        ) from exc
    except requests.RequestException as exc:
        raise UpstreamServiceError(
            f"rerank upstream failed: url={RERANK_URL} error={exc}"
        ) from exc
    results = resp.json()["results"]

    for h in hits:
        h["rerank_score"] = None
    for item in results:
        hits[int(item["index"])]["rerank_score"] = float(item["relevance_score"])

    hits.sort(key=lambda h: h["rerank_score"] if h["rerank_score"] is not None else -1e30, reverse=True)
    log(f"rerank done ({time.time() - t0:.1f}s, model={model_name}, n={len(hits)})")
    return hits[:top_k]


# ── Milvus 搜索封装 ──────────────────────────────────────────────
def _entity_field(entity, name: str):
    if hasattr(entity, "get"):
        return entity.get(name)
    return getattr(entity, name, None)


def _dense_search(col: Collection, query: str, limit: int):
    qvec = get_embed().get_query_embedding(query)
    res = col.search(
        data=[qvec],
        anns_field="embedding",
        param=DENSE_SEARCH_PARAM,
        limit=limit,
        output_fields=OUTPUT_FIELDS,
    )
    return res[0]


def _sparse_search(col: Collection, query: str, limit: int):
    res = col.search(
        data=[query],
        anns_field="sparse",
        param=SPARSE_SEARCH_PARAM,
        limit=limit,
        output_fields=OUTPUT_FIELDS,
    )
    return res[0]


def _hit_entity(hit) -> dict:
    return {
        "id": int(hit.id),
        "doc_guid": _entity_field(hit.entity, "doc_guid") or "",
        "heading": _entity_field(hit.entity, "heading") or "",
        "content": _entity_field(hit.entity, "content") or "",
        "title": _entity_field(hit.entity, "title") or "",
        "court_line": _entity_field(hit.entity, "court_line") or "",
        "title_description": _entity_field(hit.entity, "title_description") or "",
    }


# ── 对外检索接口 ──────────────────────────────────────────────────
def retrieve(
    query: str,
    config: RetrievalConfig,
    collection_name: str = DEFAULT_COLLECTION,
) -> tuple[list[dict], str]:
    col = Collection(collection_name, using=ALIAS)

    top_k = config.top_k
    candidate_top = max(top_k * CANDIDATE_MULTIPLIER, top_k)

    cos_list: list[tuple[int, float]] = []
    bm25_list: list[tuple[int, float]] = []
    entities: dict[int, dict] = {}

    if config.cos_weight > 0:
        for hit in _dense_search(col, query, candidate_top):
            nid = int(hit.id)
            cos_list.append((nid, float(hit.score)))
            entities.setdefault(nid, _hit_entity(hit))

    if config.cos_weight < 1:
        for hit in _sparse_search(col, query, candidate_top):
            nid = int(hit.id)
            bm25_list.append((nid, float(hit.score)))
            entities.setdefault(nid, _hit_entity(hit))

    if config.cos_weight == 1.0:
        fused = cos_list[:top_k]
        score_type = "cos_sim"
    elif config.cos_weight == 0.0:
        fused = bm25_list[:top_k]
        score_type = "bm25"
    else:
        fused = _weighted_sum_fuse(cos_list, bm25_list, config.cos_weight, top_k)
        score_type = "weighted_sum"

    cos_lookup = {nid: (i + 1, s) for i, (nid, s) in enumerate(cos_list)}
    bm25_lookup = {nid: (i + 1, s) for i, (nid, s) in enumerate(bm25_list)}

    # 融合时可能出现只在一路命中的 id;补拉另一路没返回的 entity。
    # 两路都搜过的场景下 entities 已经在 setdefault 阶段覆盖了所有 id。
    missing = [nid for nid, _ in fused if nid not in entities]
    if missing:
        rows = col.query(
            expr=f"id in {missing}",
            output_fields=OUTPUT_FIELDS,
        )
        for r in rows:
            entities[int(r["id"])] = {
                "id": int(r["id"]),
                "doc_guid": r.get("doc_guid") or "",
                "heading": r.get("heading") or "",
                "content": r.get("content") or "",
                "title": r.get("title") or "",
                "court_line": r.get("court_line") or "",
                "title_description": r.get("title_description") or "",
            }

    hits: list[dict] = []
    for idx, (nid, score) in enumerate(fused):
        ent = entities.get(nid, {})
        cr = cos_lookup.get(nid, (None, None))
        br = bm25_lookup.get(nid, (None, None))
        chunk_id = int(nid)
        hits.append({
            "chunk_id": chunk_id,
            "parent_id": chunk_id,
            "content": ent.get("content", ""),
            "parent": {
                "id": chunk_id,
                "doc_guid": ent.get("doc_guid") or "",
                "heading": ent.get("heading") or None,
                "content": ent.get("content", ""),
            },
            "doc": {
                "doc_guid": ent.get("doc_guid") or "",
                "title": ent.get("title") or None,
                "court_line": ent.get("court_line") or None,
                "case_preview": None,
                "title_description": ent.get("title_description") or None,
                "summary": None,
            },
            "final_rank": idx + 1,
            "final_score": float(score),
            "hybrid_score": float(score),
            "bm25_rank": br[0],
            "bm25_score": float(br[1]) if br[1] is not None else None,
            "cos_rank": cr[0],
            "cos_sim": float(cr[1]) if cr[1] is not None else None,
            "rerank_score": None,
        })

    if config.rerank_enabled:
        hits = _rerank(query, hits, RERANK_MODEL, top_k)
        score_type = "rerank"
        for new_idx, h in enumerate(hits):
            h["final_rank"] = new_idx + 1
            h["final_score"] = float(h.get("rerank_score") or 0.0)

    return hits, score_type


__all__ = [
    "RetrievalConfig",
    "retrieve",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default=DEFAULT_COLLECTION, help="Milvus 集合名。")
    ap.add_argument("--query", default=None, help="检索 query。")
    ap.add_argument("--cos-weight", dest="cos_weight", type=float, default=1.0)
    ap.add_argument("--top-k", dest="top_k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--rerank", dest="rerank_enabled", action="store_true")
    ap.add_argument("--out", nargs="?", const="", default=None)
    ap.add_argument(
        "--compare",
        nargs=2,
        metavar=("TEXT_A", "TEXT_B"),
        help="只跑相似度调试:对两段文本各编码一次并输出 cos_sim",
    )
    args = ap.parse_args()
    if not args.compare and not args.query:
        ap.error("必须指定 --query 或 --compare")

    if args.compare:
        a, b = args.compare
        va = np.asarray(get_embed().get_text_embedding(a), dtype=np.float32)
        vb = np.asarray(get_embed().get_text_embedding(b), dtype=np.float32)
        va /= (np.linalg.norm(va) or 1.0)
        vb /= (np.linalg.norm(vb) or 1.0)
        cos = float(va @ vb)
        print(f"\ncos_sim = {cos:.4f}")
        print(f"  A[{len(a)} chars]: {a[:80]}{'...' if len(a) > 80 else ''}")
        print(f"  B[{len(b)} chars]: {b[:80]}{'...' if len(b) > 80 else ''}")
        return

    config = RetrievalConfig(
        cos_weight=args.cos_weight,
        top_k=args.top_k,
        rerank_enabled=args.rerank_enabled,
    )

    milvus_connect()
    col = Collection(args.collection, using=ALIAS)
    col.load()

    results, score_type = retrieve(args.query, config, args.collection)
    for i, r in enumerate(results, 1):
        p = r["parent"] or {}
        d = r["doc"] or {}
        snippet = r["content"][:160].replace("\n", " ")
        bs = f"{r['bm25_score']:.3f}" if r["bm25_score"] is not None else "-"
        cs = f"{r['cos_sim']:.3f}" if r["cos_sim"] is not None else "-"
        hs = f"{r['hybrid_score']:.3f}"
        rs = f"{r['rerank_score']:.3f}" if r["rerank_score"] is not None else "-"
        print(
            f"\n[{i}] rank={r['final_rank']} final={r['final_score']:.4f} "
            f"hybrid={hs} rerank={rs} bm25={r['bm25_rank']}({bs}) "
            f"cos={r['cos_rank']}({cs}) doc_guid={d.get('doc_guid', '?')} "
            f"heading={p.get('heading', '')!r}"
        )
        print(f"    {snippet} ...")

    if args.out is not None:
        fname = f"retrieve_{time.strftime('%Y%m%d_%H%M%S')}.json"
        if not args.out:
            out_path = Path("data") / fname
        else:
            p = Path(args.out)
            out_path = p / fname if (p.is_dir() or args.out.endswith(("/", "\\"))) else p
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "query": args.query,
            "collection": args.collection,
            "model_name": active_embed_name(),
            "score_type": score_type,
            "retrieval_config": config.model_dump(),
            "results": results,
        }
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"results written to {out_path}")


if __name__ == "__main__":
    main()
