"""
RAG 服务入口。

启动时：
  1. 连接 Milvus 并 load 目标集合（retrieval_router.init_collection）
  2. 预热 embedding 模型（chunk_pipeline.get_embed）

挂载路由：
  retrieval_router.router  → /retrieve /health

启动：
  python main.py
  uvicorn main:app --host 0.0.0.0 --port 8080

"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()
from fastapi import FastAPI

from chunk_pipeline import get_embed, log
from retrieval_router import init_collection
from retrieval_router import router as retrieval_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    log("startup: connecting milvus + warming embedding model ...")
    t0 = time.time()
    init_collection()
    get_embed()
    log(f"startup ready ({time.time() - t0:.1f}s)")
    yield


app = FastAPI(title="RAG Service", version="0.1.0", lifespan=lifespan)
app.include_router(retrieval_router, tags=["retrieval"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=os.environ.get("UVICORN_RELOAD") == "1",
    )
