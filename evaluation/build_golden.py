"""
交互式构建 golden_dataset.jsonl。

用法:
    cd /Users/gin/Downloads/westlaw
    .venv/bin/python evaluation/build_golden.py --category general
    .venv/bin/python evaluation/build_golden.py --category patent --out evaluation/datasets/golden_patent.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from adapters import call_retrieve, RETRIEVE_URL


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evaluation/datasets/golden_dataset.jsonl")
    ap.add_argument("--category", default="general")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--cos-weight", type=float, default=0.7)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"检索服务: {RETRIEVE_URL}")
    print(f"输出文件: {out_path}  分类: {args.category}")
    print("输入 query 后从检索结果中选择相关文档，空行退出。\n")

    while True:
        query = input("Query> ").strip()
        if not query:
            break

        hits = call_retrieve(query, cos_weight=args.cos_weight, top_k=args.top_k, rerank_enabled=True)
        print(f"\n  检索到 {len(hits)} 条结果:")
        for i, h in enumerate(hits):
            doc = h["doc"]
            snippet = h["content"][:100].replace("\n", " ")
            print(f"  [{i+1:2d}] {doc.get('title_description','')[:80]}  ({doc.get('title','')})")
            print(f"        {snippet}...")

        selected = input("\n  相关文档序号（逗号分隔，如 1,3,5）: ").strip()
        if not selected:
            print("  跳过\n")
            continue

        indices = [int(x) - 1 for x in selected.split(",") if x.strip().isdigit()]
        relevant_titles = list({hits[i]["doc"].get("title_description", "") for i in indices if 0 <= i < len(hits)})

        reference_answer = input("  参考答案（可选，直接回车跳过）: ").strip() or None

        record = {
            "query": query,
            "relevant_titles": relevant_titles,
            "category": args.category,
            **({"reference_answer": reference_answer} if reference_answer else {}),
        }
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  已保存 → relevant_titles: {relevant_titles}\n")

    print("标注完成。")


if __name__ == "__main__":
    main()
