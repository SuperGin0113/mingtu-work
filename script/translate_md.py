"""
使用通义千问（qwen-plus 等）以提示词形式翻译 Markdown 文件，针对法律领域优化。

API：DashScope OpenAI 兼容接口
  https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions

用法示例：
    # 单文件
    python -m script.translate_md --input path/to/foo.md --api-key sk-xxx

    # 批量目录（递归处理 *.md，跳过已存在的 _zh 文件）
    python -m script.translate_md --input path/to/dir --api-key sk-xxx

    # 指定输出目录（镜像源目录结构）
    python -m script.translate_md --input docs/ --outdir docs_zh/ --api-key sk-xxx

    # 指定方向（默认 en -> zh）
    python -m script.translate_md --input foo.md --from zh --to en --api-key sk-xxx

    # 覆盖已存在的 _zh 输出
    python -m script.translate_md --input dir --api-key sk-xxx --overwrite

    # 自定义模型 / 端点
    python -m script.translate_md --input foo.md --model qwen-max --api-key sk-xxx

环境变量 DASHSCOPE_API_KEY 可替代 --api-key。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# 加载项目根目录的 .env（脚本位于 script/，根目录在上一级）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.5-plus"
DEFAULT_MAX_CHARS = 8000  # 每次请求送入 LLM 的最大字符数
REQUEST_TIMEOUT = 600
MAX_RETRY = 3

LANG_NAME = {
    "zh": "中文（简体）",
    "en": "英文",
    "auto": "原文语种",
}

SYSTEM_PROMPT_TMPL = (
    "你是一名资深法律翻译专家，专长于英美法系判例与法律文书的翻译。\n"
    "请将用户提供的{src_label}法律文本翻译为{tgt_label}。严格遵守以下要求：\n"
    "1. 完整保留原文的 Markdown 格式：标题层级、列表、链接、表格、代码块、引用、加粗/斜体等。\n"
    "2. 法律术语必须使用目标语言法律行业的标准译法，做到准确、专业、规范。\n"
    "3. 案件编号、法条编号、判例引用（如 'Smith v. Jones, 123 F.3d 456'）、URL、邮箱等保持原貌。\n"
    "4. 人名、地名、机构名首次出现时使用通行译法，必要时可在括号内保留原文。\n"
    "5. 保留原文段落结构与换行；不要合并或拆分段落。\n"
    "6. 直接输出译文，不添加任何解释、前言、后记、注释、Markdown 代码围栏（除非原文本身包含）。\n"
    "7. 如果遇到无法翻译的片段（如纯数字、代码、表格分隔符），原样保留。"
)


def lang_label(code: str) -> str:
    return LANG_NAME.get(code.lower(), code)


def chunk_markdown(text: str, max_chars: int) -> list[str]:
    """按段落（双换行）切块，每块 <= max_chars。超长段落退化按单换行切分。"""
    blocks = text.split("\n\n")
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""

    for blk in blocks:
        if len(blk) > max_chars:
            flush()
            # 单段落仍超长：按行切，每行单独成块；行还超长就硬切
            for line in blk.split("\n"):
                if len(line) <= max_chars:
                    if cur and len(cur) + 1 + len(line) > max_chars:
                        flush()
                    cur = f"{cur}\n{line}" if cur else line
                else:
                    flush()
                    for i in range(0, len(line), max_chars):
                        chunks.append(line[i : i + max_chars])
            flush()
            continue

        sep_len = 2 if cur else 0
        if len(cur) + sep_len + len(blk) > max_chars:
            flush()
            cur = blk
        else:
            cur = f"{cur}\n\n{blk}" if cur else blk
    flush()
    return chunks


def call_qwen(
    content: str,
    src_lang: str,
    tgt_lang: str,
    api_key: str,
    model: str,
    base_url: str,
) -> str:
    """调用 Qwen Chat Completions 翻译单个 chunk。"""
    system_prompt = SYSTEM_PROMPT_TMPL.format(
        src_label=lang_label(src_lang),
        tgt_label=lang_label(tgt_lang),
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
    }
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if "choices" not in data or not data["choices"]:
        raise RuntimeError(f"unexpected response: {str(data)[:300]}")
    return data["choices"][0]["message"]["content"]


def translate_text(
    text: str,
    src_lang: str,
    tgt_lang: str,
    api_key: str,
    model: str,
    base_url: str,
    max_chars: int,
    interval: float,
) -> str:
    chunks = chunk_markdown(text, max_chars)
    total = len(chunks)
    out_parts: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        if not chunk.strip():
            print(f"  chunk {idx}/{total} skipped (empty)")
            out_parts.append(chunk)
            continue
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                t0 = time.time()
                translated = call_qwen(chunk, src_lang, tgt_lang, api_key, model, base_url)
                elapsed = time.time() - t0
                print(
                    f"  chunk {idx}/{total} ok ({len(chunk)} chars in, "
                    f"{len(translated)} chars out, {elapsed:.1f}s)"
                )
                out_parts.append(translated)
                break
            except Exception as exc:
                last_err = exc
                wait = max(interval, 1.0) * (2 ** (attempt - 1))
                print(f"  chunk {idx}/{total} fail (attempt {attempt}/{MAX_RETRY}): {exc} -> sleep {wait:.1f}s")
                time.sleep(wait)
        else:
            raise RuntimeError(f"chunk {idx} failed after {MAX_RETRY} retries: {last_err}")
        if interval > 0 and idx < total:
            time.sleep(interval)
    return "\n\n".join(out_parts)


def translate_file(
    src_path: Path,
    out_path: Path,
    src_lang: str,
    tgt_lang: str,
    api_key: str,
    model: str,
    base_url: str,
    max_chars: int,
    interval: float,
) -> None:
    print(f"[file] {src_path} -> {out_path}")
    text = src_path.read_text(encoding="utf-8")
    if not text.strip():
        out_path.write_text(text, encoding="utf-8")
        print("  (empty content, copied as-is)")
        return

    translated = translate_text(
        text=text,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_chars=max_chars,
        interval=interval,
    )
    out_path.write_text(translated, encoding="utf-8")
    print(f"  written: {out_path}")


def collect_targets(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(p for p in input_path.rglob("*.md") if not p.stem.endswith("_zh"))
    raise FileNotFoundError(input_path)


def output_path_for(src: Path, input_root: Path, outdir: Path | None) -> Path:
    """
    无 outdir：输出与源文件同目录，加 _zh 后缀。
    有 outdir：
      - 单文件输入：outdir/<src.stem>_zh<suffix>
      - 目录输入：镜像相对路径，outdir/<rel_dir>/<src.stem>_zh<suffix>
    """
    name = f"{src.stem}_zh{src.suffix}"
    if outdir is None:
        return src.with_name(name)
    if input_root.is_file():
        return outdir / name
    rel = src.relative_to(input_root).parent
    return outdir / rel / name


def run(
    input_path: Path,
    src_lang: str,
    tgt_lang: str,
    api_key: str,
    model: str,
    base_url: str,
    max_chars: int,
    interval: float,
    overwrite: bool,
    outdir: Path | None,
    limit: int | None,
) -> None:
    targets = collect_targets(input_path)
    if limit is not None and limit > 0:
        targets = targets[:limit]
        print(f"[info] {len(targets)} markdown file(s) to process (limit={limit})")
    else:
        print(f"[info] {len(targets)} markdown file(s) to process")
    print(f"[info] model={model}  base_url={base_url}  max_chars={max_chars}")

    ok = skip = fail = 0
    for src in targets:
        out = output_path_for(src, input_path, outdir)
        if out.exists() and not overwrite:
            print(f"[skip] {out} exists (use --overwrite to redo)")
            skip += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            translate_file(
                src_path=src,
                out_path=out,
                src_lang=src_lang,
                tgt_lang=tgt_lang,
                api_key=api_key,
                model=model,
                base_url=base_url,
                max_chars=max_chars,
                interval=interval,
            )
            ok += 1
        except Exception as exc:
            print(f"[fail] {src}: {exc}")
            fail += 1

    print(f"\n[done] ok={ok}  skip={skip}  fail={fail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen-based legal translator for Markdown files")
    parser.add_argument("--input", required=True, help="md 文件路径，或包含 md 的目录（递归）")
    parser.add_argument("--outdir", default=None, help="输出目录（不传则与源文件同目录）。目录输入时镜像相对结构")
    parser.add_argument("--api-key", default=os.environ.get("DASHSCOPE_API_KEY", ""), help="DashScope API key（可用环境变量 DASHSCOPE_API_KEY）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名 (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--from", dest="src_lang", default="en", help="源语言 (default: en, 可设为 auto)")
    parser.add_argument("--to", dest="tgt_lang", default="zh", help="目标语言 (default: zh)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help=f"单次请求最大字符数 (default: {DEFAULT_MAX_CHARS})")
    parser.add_argument("--interval", type=float, default=0.0, help="每次请求间隔秒数 (default: 0)")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 _zh 文件")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件（用于测试，默认不限制）")
    args = parser.parse_args()

    if args.tgt_lang == "auto":
        sys.exit("--to 不能为 auto")
    if not args.api_key:
        sys.exit("缺少 API key：请使用 --api-key 或设置环境变量 DASHSCOPE_API_KEY")

    run(
        input_path=Path(args.input).expanduser(),
        src_lang=args.src_lang,
        tgt_lang=args.tgt_lang,
        api_key=args.api_key,
        model=args.model,
        base_url=args.base_url,
        max_chars=args.max_chars,
        interval=args.interval,
        overwrite=args.overwrite,
        outdir=Path(args.outdir).expanduser() if args.outdir else None,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
