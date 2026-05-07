"""
将 Westlaw 检索结果转换为 RAGAS SingleTurnSample（ID-based）。
通过 HTTP 调用 fto_rag 检索服务，无需直连 Milvus 或导入内部模块。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import requests
from ragas import SingleTurnSample

RETRIEVE_URL = os.environ.get("RETRIEVE_URL", "http://127.0.0.1:8000/v1/retrieve")
RETRIEVE_TIMEOUT = float(os.environ.get("RETRIEVE_TIMEOUT", "30"))


def call_retrieve(
    query: str,
    cos_weight: float,
    top_k: int,
    rerank_enabled: bool,
) -> list[dict]:
    resp = requests.post(
        RETRIEVE_URL,
        json={
            "query": query,
            "retrieval_config": {
                "cos_weight": cos_weight,
                "top_k": top_k,
                "rerank_enabled": rerank_enabled,
            },
        },
        timeout=RETRIEVE_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["results"]


def to_ragas_sample(
    query: str,
    hits: list[dict],
    relevant_titles: list[str],
    reference_answer: str | None = None,
) -> SingleTurnSample:
    retrieved_context_ids = [h["doc"].get("title_description", "") for h in hits]
    return SingleTurnSample(
        user_input=query,
        retrieved_context_ids=retrieved_context_ids,
        reference_context_ids=relevant_titles,
        response=reference_answer or "",
    )
