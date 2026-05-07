"""
Smoke test：不连 Milvus，用 mock 数据验证 RAGAS ID-based 流程是否正常。

预期结果:
  case1: recall=1.0  precision=0.5  (2/2 相关都命中，但 4 条里 2 条相关)
  case2: recall=1.0  precision=0.33 (1/1 相关命中，但 3 条里 1 条相关)
  case3: recall=0.5  precision=0.25 (doc_005 未命中，4 条里 1 条相关)

用法:
    cd /Users/gin/Downloads/westlaw
    .venv/bin/python evaluation/smoke_test.py
"""
from __future__ import annotations

from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.metrics import (  # noqa: F401  (collections path not yet exported in 0.4.3)
    _IDBasedContextPrecision as IDBasedContextPrecision,
    _IDBasedContextRecall as IDBasedContextRecall,
)

METRICS = [IDBasedContextRecall(), IDBasedContextPrecision()]

CASES = [
    {
        "query": "What is the standard of care in medical malpractice?",
        "relevant_titles": ["doc_001", "doc_002"],
        "retrieved_titles":    ["doc_001", "doc_002", "doc_decoy_a", "doc_decoy_b"],
        # recall=1.0 (2/2 命中), precision=0.5 (2/4 相关)
    },
    {
        "query": "How is patent infringement determined?",
        "relevant_titles": ["doc_003"],
        "retrieved_titles":    ["doc_003", "doc_decoy_c", "doc_decoy_d"],
        # recall=1.0 (1/1 命中), precision=0.33 (1/3 相关)
    },
    {
        "query": "What constitutes breach of contract?",
        "relevant_titles": ["doc_004", "doc_005"],
        "retrieved_titles":    ["doc_004", "doc_decoy_e", "doc_decoy_f", "doc_decoy_g"],
        # recall=0.5 (1/2 命中), precision=0.25 (1/4 相关)
    },
]


def build_sample(case: dict) -> SingleTurnSample:
    return SingleTurnSample(
        user_input=case["query"],
        retrieved_context_ids=case["retrieved_titles"],
        reference_context_ids=case["relevant_titles"],
        response="",
    )


def main() -> None:
    samples = [build_sample(c) for c in CASES]
    dataset = EvaluationDataset(samples=samples)
    result = evaluate(dataset=dataset, metrics=METRICS)
    df = result.to_pandas()
    df["query"] = [c["query"][:48] for c in CASES]
    df["expected_recall"]    = [1.0, 1.0, 0.5]
    df["expected_precision"] = [0.5, round(1/3, 4), 0.25]

    print("\n=== Smoke Test 结果 ===")
    cols = ["query", "id_based_context_recall", "expected_recall",
            "id_based_context_precision", "expected_precision"]
    print(df[cols].to_string(index=False))

    recall_ok    = all(abs(df["id_based_context_recall"][i]    - df["expected_recall"][i])    < 0.01 for i in range(len(df)))
    precision_ok = all(abs(df["id_based_context_precision"][i] - df["expected_precision"][i]) < 0.01 for i in range(len(df)))

    if recall_ok and precision_ok:
        print("\n✅ RAGAS ID-based 流程正常")
    else:
        print("\n❌ 指标与预期不符，请检查")


if __name__ == "__main__":
    main()
