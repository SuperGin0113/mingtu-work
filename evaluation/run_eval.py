"""
Westlaw 检索评测主脚本（基于 RAGAS ID-based 指标，title 精确匹配）。

用法:
    cd /Users/gin/Downloads/westlaw
    .venv/bin/python evaluation/run_eval.py
    .venv/bin/python evaluation/run_eval.py --dataset evaluation/datasets/golden_dataset.jsonl
    .venv/bin/python evaluation/run_eval.py --configs pure_vector hybrid_rerank
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from ragas import EvaluationDataset, evaluate
from ragas.metrics import (  # noqa: F401  (collections path not yet exported in 0.4.3)
    _IDBasedContextPrecision as IDBasedContextPrecision,
    _IDBasedContextRecall as IDBasedContextRecall,
)

from adapters import call_retrieve, to_ragas_sample

METRICS = [IDBasedContextRecall(), IDBasedContextPrecision()]
RECALL_COL    = "id_based_context_recall"
PRECISION_COL = "id_based_context_precision"

# 对比配置：纯向量 / 纯 BM25 / 混合 / 混合+重排
@dataclass
class EvalConfig:
    name: str
    cos_weight: float
    top_k: int
    rerank_enabled: bool


CONFIGS: list[EvalConfig] = [
    EvalConfig("pure_vector",   cos_weight=1.0, top_k=10, rerank_enabled=False),
    EvalConfig("pure_bm25",     cos_weight=0.0, top_k=10, rerank_enabled=False),
    EvalConfig("hybrid_0.7",    cos_weight=0.7, top_k=10, rerank_enabled=False),
    EvalConfig("hybrid_rerank", cos_weight=0.7, top_k=10, rerank_enabled=True),
]


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def eval_one_config(cfg: EvalConfig, golden: list[dict]) -> pd.DataFrame:
    samples = []
    for item in golden:
        hits = call_retrieve(
            item["query"],
            cos_weight=cfg.cos_weight,
            top_k=cfg.top_k,
            rerank_enabled=cfg.rerank_enabled,
        )
        sample = to_ragas_sample(
            item["query"], hits, item["relevant_titles"],
            item.get("reference_answer"),
        )
        samples.append(sample)

    dataset = EvaluationDataset(samples=samples)
    result = evaluate(dataset=dataset, metrics=METRICS)
    df = result.to_pandas()
    df["config"]   = cfg.name
    df["query"]    = [item["query"] for item in golden]
    df["category"] = [item.get("category", "general") for item in golden]
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="evaluation/datasets/golden_dataset.jsonl")
    ap.add_argument("--output",  default="evaluation/reports/")
    ap.add_argument("--configs", nargs="*", help="只跑指定配置名，默认全跑")
    args = ap.parse_args()

    golden = load_golden(args.dataset)
    print(f"[eval] 测试集: {len(golden)} 条  来源: {args.dataset}")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    configs = [c for c in CONFIGS if not args.configs or c.name in args.configs]
    all_dfs: list[pd.DataFrame] = []

    for cfg in configs:
        print(f"\n[eval] 配置: {cfg.name}  cos_weight={cfg.cos_weight}  rerank={cfg.rerank_enabled}")
        t0 = time.time()
        df = eval_one_config(cfg, golden)
        elapsed = time.time() - t0
        all_dfs.append(df)
        mean_r = df[RECALL_COL].mean()
        mean_p = df[PRECISION_COL].mean()
        print(f"  recall={mean_r:.4f}  precision={mean_p:.4f}  耗时={elapsed:.1f}s")

    full = pd.concat(all_dfs, ignore_index=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    detail_path = out_dir / f"detail_{ts}.csv"
    full.to_csv(detail_path, index=False)

    summary = (
        full.groupby(["config", "category"])[[RECALL_COL, PRECISION_COL]]
        .mean()
        .round(4)
    )
    summary_path = out_dir / f"summary_{ts}.csv"
    summary.to_csv(summary_path)

    print("\n" + "=" * 60)
    print("汇总（按配置 × 类别）:")
    print(summary.to_string())
    print(f"\n详细结果 → {detail_path}")
    print(f"汇总结果 → {summary_path}")


if __name__ == "__main__":
    main()
