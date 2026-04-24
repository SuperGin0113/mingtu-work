"""
Westlaw 检索路由(APIRouter)。

挂载到 main.py 的 FastAPI app。接口:
  POST /retrieve  —— 主检索(query → top-K 命中 + 文档元信息;可选重排)
  GET  /health    —— 存活

环境变量:
  MILVUS_COLLECTION   Milvus 集合名,默认 chunks_test(见 milvus_db.DEFAULT_COLLECTION)
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pymilvus import Collection, utility

from chunk_pipeline import UpstreamServiceError
from milvus_db import ALIAS, DEFAULT_COLLECTION, connect as milvus_connect
from retrieval_pipeline import RetrievalConfig, retrieve

PARENT_CONTENT_PREVIEW = 1000

_state: dict = {"ready": False, "loaded_at": None}


def init_collection() -> None:
    milvus_connect()
    if not utility.has_collection(DEFAULT_COLLECTION, using=ALIAS):
        raise RuntimeError(
            f"milvus collection {DEFAULT_COLLECTION} not found; "
            "run `python milvus_db.py --init` and ingest data first"
        )
    col = Collection(DEFAULT_COLLECTION, using=ALIAS)
    col.load()
    _state["ready"] = True
    _state["loaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


router = APIRouter()


# ── schemas ──────────────────────────────────────────────────────
class RetrieveReq(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="用户检索问题或关键词->与判例库文档内容进行检索。",
    )
    retrieval_config: RetrievalConfig = Field(
        default_factory=RetrievalConfig,
        description="检索配置;cos_weight 控制向量/BM25 权重(1.0=纯向量,0.0=纯 BM25),top_k 控制返回条数(默认 30),rerank_enabled 控制是否重排。",
    )


class DocMeta(BaseModel):
    doc_guid: str = Field(description="文档 guid,对应 doc_items.doc_guid。")
    title: str | None = Field(default=None, description="文档标题。")
    court_line: str | None = Field(
        default=None,
        description="法院信息,对应 doc_items.court_line。",
    )
    title_description: str | None = Field(default=None, description="标题补充描述。")


class ParentCtx(BaseModel):
    id: int = Field(description="父块主键 id。")
    doc_guid: str = Field(description="所属文档 guid,对应 doc_items.doc_guid。")
    heading: str | None = Field(default=None, description="父块标题,如 Opinion、Headnotes。")
    content: str = Field(description=f"父块文本,默认截取前 {PARENT_CONTENT_PREVIEW} 字符作为上位上下文。")


class Hit(BaseModel):
    chunk_id: int = Field(description="命中的子块 id。")
    parent_id: int = Field(description="命中子块对应的父块 id。")
    content: str = Field(description="命中的子块文本。")
    parent: ParentCtx | None = Field(default=None, description="父块上下文(content 截取前若干字符)。")
    doc: DocMeta | None = Field(default=None, description="命中文档的补充元信息。")
    final_rank: int = Field(description="最终排序名次,从 1 开始。")
    final_score: float = Field(description="最终排序分数,数值越大越相关。具体来源见响应顶层 score_type。")
    hybrid_score: float = Field(description="融合阶段分数(weighted_sum / 纯向量 cos_sim / 纯 BM25 原分),重排前的排序依据。")
    bm25_rank: int | None = Field(default=None, description="BM25 排名,从 1 开始。")
    bm25_score: float | None = Field(default=None, description="BM25 原始分数。")
    cos_rank: int | None = Field(default=None, description="向量相似度排名,从 1 开始。")
    cos_sim: float | None = Field(default=None, description="向量余弦相似度。")
    rerank_score: float | None = Field(default=None, description="重排分数,未启用时为 null。")


class RetrieveResp(BaseModel):
    query: str = Field(description="本次检索的原始 query。")
    took_ms: int = Field(description="本次检索耗时,单位毫秒。")
    score_type: str = Field(description="results[i].score 的打分来源;cos_sim / bm25 / weighted_sum / rerank。")
    retrieval_config: RetrievalConfig = Field(description="本次请求生效的检索配置(回显)。")
    results: list[Hit] = Field(description="检索结果列表;默认返回前 30 条,可通过 retrieval_config.top_k 调整。")


class HealthResp(BaseModel):
    status: str = Field(description="服务状态,ok 或 loading。")


# ── endpoints ────────────────────────────────────────────────────
@router.post("/retrieve", response_model=RetrieveResp)
def api_retrieve(req: RetrieveReq):
    if not _state["ready"]:
        raise HTTPException(status_code=503, detail="milvus collection not ready")
    t0 = time.time()
    try:
        hits, score_type = retrieve(req.query, req.retrieval_config, DEFAULT_COLLECTION)
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    for h in hits:
        p = h.get("parent")
        if p and p.get("content"):
            p["content"] = p["content"][:PARENT_CONTENT_PREVIEW]
    return RetrieveResp(
        query=req.query,
        took_ms=int((time.time() - t0) * 1000),
        score_type=score_type,
        retrieval_config=req.retrieval_config,
        results=hits,
    )


@router.get("/health", response_model=HealthResp)
def api_health():
    return HealthResp(status="ok" if _state["ready"] else "loading")
