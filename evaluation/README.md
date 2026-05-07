# Westlaw 检索评测指南

基于 [RAGAS](https://docs.ragas.io/) 框架，对 Westlaw 检索管线（Dense + BM25 混合检索 + 可选重排）进行离线定量评测。

## 环境要求

- Python 3.11+
- fto_rag 检索服务运行中（默认 `http://127.0.0.1:8000`，可通过 `.env` 中 `RETRIEVE_URL` 覆盖）

## 一次性安装

```bash
cd /Users/gin/Downloads/westlaw

# 创建虚拟环境（已创建可跳过）
python3 -m venv .venv

# 安装依赖
.venv/bin/pip install ragas
.venv/bin/pip install -r requirements.txt
```

---

## 第一步：构建测试集

运行交互式标注工具，逐条输入 query，从检索结果里选出相关文档。

```bash
.venv/bin/python evaluation/build_golden.py \
    --category general \
    --out evaluation/datasets/golden_dataset.jsonl
```

**参数说明**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--out` | `evaluation/datasets/golden_dataset.jsonl` | 输出文件路径（追加写入） |
| `--category` | `general` | 当前 query 的法律类别标签，便于分类分析 |
| `--top-k` | `20` | 标注时检索多少条候选 |
| `--cos-weight` | `0.7` | 标注时使用的混合权重 |

**交互流程示例**

```
Query> What is the standard for negligence in medical malpractice?

  检索到 20 条结果:
  [ 1] guid=abc123  Smith v. Hospital (2021)
        The standard of care requires physicians to act as...
  [ 2] guid=def456  Jones v. Clinic (2019)
        Medical negligence is established when a provider fails...
  ...

  相关文档序号（逗号分隔，如 1,3,5）: 1,2
  参考答案（可选，直接回车跳过）: 
  已保存 → relevant_titles: ['Smith v. Hospital (2021)', 'Jones v. Clinic (2019)']
```

> 空行退出。每条记录立即追加写入文件，中途退出不丢数据。

**不同法律类别分别建集**

```bash
.venv/bin/python evaluation/build_golden.py --category medical_malpractice \
    --out evaluation/datasets/golden_medical.jsonl

.venv/bin/python evaluation/build_golden.py --category patent \
    --out evaluation/datasets/golden_patent.jsonl
```

---

## 第二步：运行评测

```bash
.venv/bin/python evaluation/run_eval.py
```

默认读取 `evaluation/datasets/golden_dataset.jsonl`，对比以下 4 种配置：

| 配置名 | cos_weight | rerank | 说明 |
|--------|-----------|--------|------|
| `pure_vector` | 1.0 | 否 | 纯向量检索（BGE-M3 COSINE） |
| `pure_bm25` | 0.0 | 否 | 纯 BM25 关键词检索 |
| `hybrid_0.7` | 0.7 | 否 | 混合检索（向量权重 0.7） |
| `hybrid_rerank` | 0.7 | 是 | 混合 + BGE-Reranker 重排 |

**自定义参数**

```bash
# 指定测试集文件
.venv/bin/python evaluation/run_eval.py \
    --dataset evaluation/datasets/golden_medical.jsonl

# 只跑部分配置（对比用）
.venv/bin/python evaluation/run_eval.py \
    --configs pure_vector hybrid_rerank

# 自定义输出目录
.venv/bin/python evaluation/run_eval.py \
    --output evaluation/reports/medical/
```

---

## 指标说明

| 指标 | 含义 | 范围 | 越高越好 |
|------|------|------|---------|
| `id_based_context_recall` | ground truth 的 title 有多少比例被召回 | 0–1 | ✅ |
| `id_based_context_precision` | 召回结果中有多少比例是相关文档 | 0–1 | ✅ |

两个指标均基于 `title` **精确匹配**（非字符串相似度），**无需调用 LLM**，结果完全可复现。

**计算方式**：
```
recall    = 命中的相关 title 数  /  标注的相关 title 总数
precision = 命中的相关 title 数  /  检索返回的总条数
```

---

## 输出结果

评测结束后自动生成两个文件到 `evaluation/reports/`：

```
evaluation/reports/
├── detail_20260426_153000.csv    # 每条 query × 每个配置的原始分数
└── summary_20260426_153000.csv   # 按配置 × 类别汇总的均值
```

**summary 示例**

```
config           category             recall   precision
pure_vector      general              0.62     0.58
pure_bm25        general              0.55     0.51
hybrid_0.7       general              0.71     0.67
hybrid_rerank    general              0.78     0.74
```

---

## 目录结构

```
evaluation/
├── README.md              本文档
├── adapters.py            检索调用 + RAGAS 格式转换
├── build_golden.py        交互式标注工具
├── run_eval.py            评测主脚本
├── datasets/
│   └── golden_dataset.jsonl   测试集（每行一条 JSON）
└── reports/
    ├── detail_*.csv
    └── summary_*.csv
```

## 测试集格式参考

```jsonl
{"query": "...", "relevant_titles": ["Smith v. Hospital (2021)", "Jones v. Clinic (2019)"], "category": "general"}
{"query": "...", "relevant_titles": ["728 F.2d 1423"], "category": "patent", "reference_answer": "..."}
```

- `query`：必填，检索问题
- `relevant_titles`：必填，人工标注的相关文档 title 列表
- `category`：可选，法律类别标签，用于分类分析
- `reference_answer`：可选，预留给后续接入 LLM 生成评测时使用
