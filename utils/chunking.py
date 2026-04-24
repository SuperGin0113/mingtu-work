from __future__ import annotations

import os
import re
from pathlib import Path

# HF 镜像（在 import transformers / llama_index 之前设置才生效）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

CHUNK_TOKENS = 512
CHUNK_OVERLAP = 50
HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)
HF_HUB_DIR = Path.home() / ".cache" / "huggingface" / "hub"
TOKENIZER_MODEL = os.environ.get("TOKENIZER_MODEL", "BAAI/bge-m3")

_tokenizer = None
_splitter = None


def split_parents(md: str) -> list[dict]:
    md = md or ""
    ms = list(HEADING_RE.finditer(md))
    if not ms:
        body = md.strip()
        return [{"heading": "", "body": body, "start": 0, "end": len(md)}] if body else []
    out: list[dict] = []
    preamble = md[: ms[0].start()].strip()
    for i, m in enumerate(ms):
        start = m.start()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        body = md[start:end].rstrip()
        if i == 0 and preamble:
            body = preamble + "\n\n" + body
            start = 0
        out.append({"heading": m.group(1).strip(), "body": body, "start": start, "end": end})
    return out


def resolve_local_model_path(model_name: str) -> str | None:
    parts = model_name.split("/", 1)
    if len(parts) != 2:
        return None

    model_dir = HF_HUB_DIR / f"models--{parts[0]}--{parts[1]}"
    refs_main = model_dir / "refs" / "main"
    if refs_main.exists():
        ref = refs_main.read_text(encoding="utf-8").strip()
        snap = model_dir / "snapshots" / ref
        if snap.exists() and any(snap.iterdir()):
            return str(snap)

    snapshots_dir = model_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    candidates = [p for p in snapshots_dir.iterdir() if p.is_dir() and any(p.iterdir())]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(latest)


def _resolve_tokenizer(name: str) -> tuple[str, dict]:
    local = resolve_local_model_path(name)
    if local:
        return local, {"local_files_only": True}
    return name, {}


def get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer

        src, kw = _resolve_tokenizer(TOKENIZER_MODEL)
        _tokenizer = AutoTokenizer.from_pretrained(src, **kw)
    return _tokenizer


def get_splitter():
    global _splitter
    if _splitter is None:
        from llama_index.core.node_parser import SentenceSplitter

        _splitter = SentenceSplitter(
            chunk_size=CHUNK_TOKENS,
            chunk_overlap=CHUNK_OVERLAP,
            tokenizer=get_tokenizer().encode,
        )
    return _splitter


def tok_len(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False))
