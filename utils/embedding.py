from __future__ import annotations

import os

# 确保 HF 相关 env 在 transformers / llama_index 加载前就位
from .chunking import HF_HUB_DIR  # noqa: F401  (import triggers env setup)
from .errors import UpstreamServiceError
from .logging import log

EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "20"))
EMBED_URL = os.environ.get("EMBED_URL")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT", "60"))

_embed = None


def active_embed_name() -> str:
    return EMBED_MODEL


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
