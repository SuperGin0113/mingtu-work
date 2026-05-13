# RAG Serve


## 目录

```text
├── main.py                        # FastAPI 入口
├── retrieval_router.py            # /retrieve /health
├── retrieval_pipeline.py          # Milvus 向量 + BM25 融合
├── chunk/
│   ├── chunk_pipelineV2.py        # 切块 + embedding 写入 Milvus
│   └── html2md_pipeline.py        # HTML → Markdown
├── milvus_db.py                   # Milvus 集合初始化
└── script/                        # 抓取、下载、导出脚本（含 pgsql db.py）
```

## 常用命令

```bash
# 初始化 Milvus 集合
python milvus_db.py --init

# 切块 + 入库
python -m chunk.chunk_pipelineV2 --batch

# 启动检索服务
python main.py
```

## 检索接口

`POST /retrieve`

```json
{
  "query": "string",
  "retrieval_config": {
    "cos_weight": 1.0,
    "top_k": 30,
    "rerank_enabled": false
  }
}
```

- `cos_weight=1.0` 纯向量，`0.0` 纯 BM25，中间值加权融合
- `rerank_enabled` 需配置 `RERANK_URL` / `RERANK_MODEL`

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `TEST_MILVUS_HOST` / `TEST_MILVUS_PORT` | Milvus 地址 |
| `TEST_MILVUS_TOKEN` 或 `_USER`/`_PASSWORD` | Milvus 鉴权 |
| `MILVUS_COLLECTION` | 集合名，默认 `chunks_test` |
| `EMBED_URL` / `EMBED_MODEL` | embedding 服务 |
| `RERANK_URL` / `RERANK_MODEL` | 重排服务 |
