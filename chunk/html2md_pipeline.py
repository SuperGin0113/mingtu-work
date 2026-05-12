"""
Westlaw HTML 两阶段处理管线。

两种运行模式：
  文件系统（默认）—— 扫描 data/doc_html_output/ 逐文件产出清洗 HTML + Markdown
  DB —— 读 doc_items.doc_html，写回 doc_html_clean / doc_md_clean

CLI:
    python -m chunk.html2md_pipeline                      # 文件系统全阶段
    python -m chunk.html2md_pipeline --mode clean         # 文件系统仅清洗
    python -m chunk.html2md_pipeline --mode md            # 文件系统仅 Markdown
    python -m chunk.html2md_pipeline --mode db-row --id 42
    python -m chunk.html2md_pipeline --mode db-batch              # 跑 clean_process_status IN (0, 3)
    python -m chunk.html2md_pipeline --mode db-batch --force      # 全量重跑
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as _html_to_markdown


# ── 路径常量 ───────────────────────────────────────────────────────
DATA_DIR = Path("data")
RAW_HTML_DIR = DATA_DIR / "doc_html_output"
CLEAN_HTML_DIR = DATA_DIR / "doc_html_output_no_request"
MD_DIR = DATA_DIR / "doc_md_output"
CONTENT_IMAGES_INDEX = Path("content_images") / "content_images.json"


# ── 清洗阶段常量 ───────────────────────────────────────────────────
REMOVE_IDS = {
    "co_docToolbar",
    "co_docToolbarBottom",
    "co_docPrimaryTabNavigationContainer",
    "co_docAnnotationsToolbar",
    "co_docHeaderNegativeTreatment",
    "co_documentContentCacheKey",
    "co_persistBottom",
    "co_docDeliveryWidget",
    "co_docHeaderContainer",
    "co_endOfDocument",
}

REMOVE_CLASS_PATTERNS = (
    re.compile(r"\bco_fancyKeycite", re.I),
    re.compile(r"\bco_keyIcon\b", re.I),
    re.compile(r"\bco_headnoteTopics\b", re.I),
    re.compile(r"\bco_headnoteTopicsCell\b", re.I),
    re.compile(r"\bco_headnoteCitedCaseRef\b", re.I),
    re.compile(r"\bco_document_indicators\b", re.I),
    re.compile(r"\bco_docDelivery_dottedLine\b", re.I),
    re.compile(r"\bco_documentReportSection\b", re.I),
    re.compile(r"\bco_construedTerms\b", re.I),
    re.compile(r"\bco_tabNavigation\b", re.I),
    re.compile(r"\bco_snapSnippet\b", re.I),
    re.compile(r"\bco_docNav\b", re.I),
    re.compile(r"\bco_starPage\b", re.I),
    re.compile(r"\bco_starPageMetadataItem\b", re.I),
)

REMOVE_TAGS = {
    "script", "style", "link", "noscript", "iframe", "svg", "form",
    "object", "embed", "template",
}

REMOTE_PREFIXES = ("http://", "https://", "//")
NONLOCAL_PREFIXES = REMOTE_PREFIXES + ("javascript:", "mailto:", "/")
# 打在 <img> 上的标记类：属性扫除阶段看到这个类就不要删它的 src
KEEP_REMOTE_IMG_CLASS = "offline-keep-remote-image"


# ── Markdown 阶段常量 ──────────────────────────────────────────────
# 兜底再清理：离线 HTML 理论上已经清掉这些节点，这里保留是为了防御式处理
_MD_DROP_SELECTORS = [
    "script", "style", "noscript", "iframe", "svg", "form",
    "nav", "header", "footer",
    ".co_citatorFlag",
    ".co_documentStatusIcons",
    ".co_headnoteRanking",
    ".co_headnoteFooter",
    ".co_primaryHeadnoteNodes",
    ".co_headnoteClassification",
    ".co_headnoteHierarchy",
    ".co_kcHeadnoteFooter",
    ".co_displayKeyNumberTopics",
    ".co_keyNumberSymbol",
    ".co_khSpeedRead",
]

# 转换后行级噪声：Westlaw 在正文里夹带的 UI 文案 / Key Number 分类编号
_MD_NOISE_PATTERNS = [
    r"^Cases that cite this headnote\s*$",
    r"^View Headnote.*$",
    r"^\d{3}[A-Z][A-Za-z0-9()\s\-]+$",  # 291VIII Design Patents、291k2055……
    r"^Skip Page Header\s*$",
    r"^Toggle Menu\s*$",
]
_MD_NOISE_RE = re.compile("|".join(_MD_NOISE_PATTERNS), re.MULTILINE)

# 从 Westlaw 图床 URL 抽 blob_id：
# https://.../Link/Document/Blob/I26bdc070...f1aa68a93c8c3508d7.png?...
_BLOB_ID_RE = re.compile(r"/Blob/([^.?/]+)")



# ══════════════════════════════════════════════════════════════════
# 阶段 1：HTML 清洗
# ══════════════════════════════════════════════════════════════════

def normalize_doc_key(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"^\d+_", "", stem).strip()


def is_nonlocal_link(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith(NONLOCAL_PREFIXES)


def _class_text(tag: Tag) -> str:
    if getattr(tag, "attrs", None) is None:
        return ""
    classes = tag.get("class", [])
    if isinstance(classes, str):
        return classes
    return " ".join(classes)


def _should_remove_for_class(tag: Tag) -> bool:
    if getattr(tag, "attrs", None) is None:
        return False
    joined = _class_text(tag)
    return any(pattern.search(joined) for pattern in REMOVE_CLASS_PATTERNS)


def safe_title_from_soup(soup: BeautifulSoup, fallback: str) -> str:
    title_node = soup.select_one("#title")
    if title_node:
        text = title_node.get_text(" ", strip=True)
        if text:
            return text

    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(" ", strip=True)
        if text:
            return text.split("|", 1)[0].strip()

    return fallback


def load_local_image_index(base_dir: Path) -> dict[str, dict[str, Path]]:
    index_path = base_dir / CONTENT_IMAGES_INDEX
    if not index_path.exists():
        return {}

    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    by_doc: dict[str, dict[str, Path]] = {}
    for item in raw.get("images", []):
        local_path_value = item.get("local_path")
        if not local_path_value:
            continue

        local_path = base_dir / local_path_value
        if not local_path.exists():
            continue

        doc_key = normalize_doc_key(item.get("doc_title", ""))
        doc_map = by_doc.setdefault(doc_key, {})

        src = item.get("src")
        if src:
            doc_map[src] = local_path

        blob_id = item.get("blob_id")
        if blob_id:
            doc_map[blob_id] = local_path

    return by_doc


def _resolve_local_image(doc_images: dict[str, Path], src: str) -> Path | None:
    if not src:
        return None

    if src in doc_images:
        return doc_images[src]

    match = _BLOB_ID_RE.search(src)
    if match:
        return doc_images.get(match.group(1))

    return None


def _remove_elements(root: Tag, selector_ids: Iterable[str]) -> None:
    for element_id in selector_ids:
        for node in list(root.find_all(id=element_id)):
            node.decompose()


def _sanitize_html_to_page(
    html: str,
    doc_key: str,
    output_parent: Path | None,
    image_index: dict[str, dict[str, Path]],
) -> tuple[str, str, int]:
    """核心清洗逻辑。返回 (完整页面 HTML, 标题, 替换本地图数)。

    output_parent is None 时跳过本地图替换（DB 模式），图片保留远程 URL。
    """
    soup = BeautifulSoup(html, "html.parser")
    title = safe_title_from_soup(soup, doc_key)

    source_root = (
        soup.select_one("#co_document")
        or soup.select_one("#co_contentColumn")
        or soup.body
    )
    if source_root is None:
        raise RuntimeError(f"No usable document root found in {doc_key}")

    fragment_soup = BeautifulSoup(str(source_root), "html.parser")
    root = fragment_soup.find()
    if root is None:
        raise RuntimeError(f"Failed to parse content root from {doc_key}")

    _remove_elements(root, REMOVE_IDS)

    for tag_name in REMOVE_TAGS:
        for node in list(root.find_all(tag_name)):
            node.decompose()

    for node in list(root.find_all(True)):
        if _should_remove_for_class(node):
            node.decompose()

    for node in list(root.find_all(True)):
        style = node.get("style", "")
        if node.has_attr("hidden") or re.search(r"display\s*:\s*none", style, flags=re.I):
            node.decompose()

    for node in list(root.find_all("input")):
        node.decompose()

    for button in list(root.find_all("button")):
        button.name = "span"
        for attr in list(button.attrs):
            if attr not in {"class", "id"}:
                del button[attr]

    for anchor in list(root.find_all("a")):
        href = (anchor.get("href") or "").strip()
        classes = anchor.get("class") or []
        # Opinion→headnote back-refs render as bare numbers and cluster
        # like "202122We have discretion..." at the start of paragraphs.
        if "co_headnoteLink" in classes:
            anchor.decompose()
            continue
        if href.startswith("#"):
            for attr in list(anchor.attrs):
                if attr not in {"href", "id", "class", "title", "aria-label"}:
                    del anchor[attr]
            continue
        anchor.unwrap()

    doc_images = image_index.get(normalize_doc_key(doc_key), {})
    replaced_images = 0

    for img in list(root.find_all("img")):
        src = (img.get("src") or "").strip()
        in_figure = img.find_parent("div", class_="x_figure") is not None

        if in_figure:
            local_image = (
                _resolve_local_image(doc_images, src) if output_parent is not None else None
            )
            if local_image is not None:
                relative_src = os.path.relpath(local_image, output_parent).replace("\\", "/")
                img.attrs = {
                    "src": relative_src,
                    "alt": img.get("alt", ""),
                    "loading": "lazy",
                }
                replaced_images += 1
            else:
                # 没有本地文件就保留原 Westlaw 远程 URL；打标记类以便属性扫除放行 src
                img.attrs = {
                    "src": src,
                    "alt": img.get("alt", ""),
                    "loading": "lazy",
                    "class": KEEP_REMOTE_IMG_CLASS,
                }
            continue

        img.decompose()

    for node in list(root.find_all(True)):
        is_preserved_img = (
            node.name == "img"
            and KEEP_REMOTE_IMG_CLASS in (node.get("class") or [])
        )
        for attr in list(node.attrs):
            value = node.attrs.get(attr)

            if attr.lower().startswith("on"):
                del node[attr]
                continue

            if attr in {"srcset", "integrity", "crossorigin", "nonce", "referrerpolicy"}:
                del node[attr]
                continue

            if isinstance(value, str) and attr in {"src", "href", "data-href", "poster", "action"}:
                if is_preserved_img and attr == "src":
                    continue
                if is_nonlocal_link(value):
                    del node[attr]

    html_body = str(root)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
</head>
<body>
  <main>
    <section>
      <p><strong>Offline readable copy.</strong> External scripts, styles, icons, and remote links were stripped. Content images use local files when available; otherwise the original Westlaw image URL is preserved (loads only when online).</p>
{html_body}
    </section>
  </main>
</body>
</html>
"""
    return page, title, replaced_images


def sanitize_fragment(
    source_html: Path,
    output_html: Path,
    image_index: dict[str, dict[str, Path]],
) -> tuple[str, int]:
    """文件系统模式：读 source_html 并返回清洗后的完整页面。"""
    page, _title, replaced = _sanitize_html_to_page(
        source_html.read_text(encoding="utf-8"),
        doc_key=source_html.stem,
        output_parent=output_html.parent,
        image_index=image_index,
    )
    return page, replaced


def sanitize_html_string(html: str, doc_key: str = "") -> tuple[str, str]:
    """DB 模式：纯字符串清洗，不做本地图替换。返回 (页面 HTML, 标题)。"""
    page, title, _ = _sanitize_html_to_page(
        html, doc_key=doc_key, output_parent=None, image_index={}
    )
    return page, title


def build_index(output_dir: Path, pages: list[tuple[str, str]]) -> None:
    list_items = "\n".join(
        f'        <li><a href="{filename}">{title}</a></li>' for filename, title in pages
    )
    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Offline Westlaw HTML Copies</title>
</head>
<body>
  <main>
    <section>
      <h1>Offline HTML Copies</h1>
      <p><strong>Directory purpose.</strong> These files were generated from the original Westlaw exports as local-readable copies with external resource attributes removed.</p>
      <ol>
{list_items}
      </ol>
    </section>
  </main>
</body>
</html>
"""
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")


def clean_single_file(
    source_html: Path,
    clean_html: Path,
    image_index: dict[str, dict[str, Path]],
) -> tuple[str, int]:
    """清洗单个文件，返回 (页面标题, 替换的本地图片数)。"""
    page_html, replaced = sanitize_fragment(source_html, clean_html, image_index)
    clean_html.write_text(page_html, encoding="utf-8")
    soup = BeautifulSoup(page_html, "html.parser")
    title = safe_title_from_soup(soup, source_html.stem)
    return title, replaced


# ══════════════════════════════════════════════════════════════════
# 阶段 2：Markdown 转换
# ══════════════════════════════════════════════════════════════════

def _strip_noise_within_fragment(root) -> None:
    """在单个片段内做最后一道清洗：脱属性、砍锚点包装、移除短标题。"""
    for key_text in root.select(
        ".co_primaryHeadnoteNodes > .co_lastKeyText, "
        ".co_secondaryHeadnoteNodes > .co_lastKeyText"
    ):
        prev = key_text.previous_sibling
        while isinstance(prev, NavigableString) and not prev.strip():
            prev = prev.previous_sibling
        if prev is not None:
            key_text.insert_before(" ")

    # co_starPage / metadata item 是页码标记，正文没用
    for star in root.find_all(class_="co_starPage"):
        star.decompose()
    for meta in root.find_all(class_="co_starPageMetadataItem"):
        meta.decompose()
    # 脚注 / 交互弹窗用的 <button>，保留文本即可
    for btn in root.find_all("button"):
        btn.unwrap()
    # 原始标题会被我们自己的 ## 头部替换，这里直接丢掉
    for h in root.find_all(class_="co_printHeading"):
        h.decompose()
    for h in root.find_all(["h1", "h2"]):
        if len(h.get_text(strip=True)) < 40:
            h.decompose()
    # 正文里的脚注角标：<sup><span class=co_footnoteReference>2</span></sup>
    # → [^2]（Markdown 脚注引用语法）
    for sup in root.find_all("sup"):
        ref_span = sup.find(class_="co_footnoteReference")
        if ref_span is not None:
            num = ref_span.get_text(strip=True)
            if num:
                sup.replace_with(f"[^{num}]")
    # 页内锚点保留为 [文本]，外链锚点直接展开成纯文本
    for a in root.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.startswith("#"):
            a.replace_with(f"[{a.get_text(strip=True)}]")
        else:
            a.unwrap()
    # 属性大扫除，避免污染 markdownify 输出
    drop_attrs = (
        "style", "onclick", "onload", "aria-label", "role",
        "target", "rel", "id", "class", "lang", "title",
        "data-v", "data-id",
    )
    for el in root.find_all(True):
        for attr in drop_attrs:
            el.attrs.pop(attr, None)
    # 图片：UI 图标直接丢；内容图输出成 ![图:alt](blobid:xxx)，blob_id 从 src 里抽
    for img in root.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if not alt or any(k in alt.lower()
                          for k in ("key number", "eyeglasses", "display")):
            img.decompose()
            continue
        src = (img.get("src") or "").strip()
        match = _BLOB_ID_RE.search(src)
        if match:
            target = f"blobid:{match.group(1)}"
        elif src:
            target = src
        else:
            img.replace_with(f"[图:{alt}]")
            continue
        img.replace_with(f"![图:{alt}]({target})")


def _fragment_to_md(fragment) -> str:
    """把一个清洗后的 BeautifulSoup 片段转成 Markdown 文本。"""
    if fragment is None:
        return ""
    text = _html_to_markdown(
        str(fragment), heading_style="ATX", bullets="-", strip=["input"]
    )
    text = re.sub(r"(?m)^(\*\*.+?\*\*)(?=[A-Za-z0-9])", r"\1 ", text)
    text = _MD_NOISE_RE.sub("", text)
    # 去掉零宽字符 / BOM、非断行空格、行尾空白
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_html_to_md_text(html: str) -> str:
    """核心转换：清洗后的页面 HTML → 分节 Markdown 文本。"""
    soup = BeautifulSoup(html, "html.parser")
    doc = soup.find(id="co_document")
    if doc is None:
        raise ValueError("页面中找不到 #co_document 根节点")

    # 兜底再清一遍（离线 HTML 理论上已经处理过）
    for selector in _MD_DROP_SELECTORS:
        for el in doc.select(selector):
            el.decompose()

    # ── 元数据（法院 / 案号 / 当事人 / 日期 / 引文）──────────────
    meta_parts: list[str] = []
    for cls in ("co_cites", "co_courtBlock", "co_partyLine",
                "co_docketBlock", "co_date"):
        for el in doc.find_all(class_=cls):
            text = el.get_text(" ", strip=True)
            if text:
                meta_parts.append(text)
    seen: set[str] = set()
    meta: list[str] = []
    for part in meta_parts:
        if part not in seen:
            seen.add(part)
            meta.append(part)

    # ── 先抓片段再清洗，避免清洗阶段把定位用的 class 弄掉 ───────
    synopsis = doc.find(class_="co_synopsis")
    headnotes = doc.find(class_="co_headnotes")
    opinion_blocks = list(doc.find_all(class_="co_opinionBlock"))
    footnote_pairs: list[tuple[str, object]] = []
    for body in doc.find_all(class_="co_footnoteBody"):
        wrapper = body.parent
        num_el = wrapper.find(class_="co_footnoteNumber") if wrapper else None
        num = num_el.get_text(strip=True) if num_el else ""
        footnote_pairs.append((num, body))

    for frag in (synopsis, headnotes, *opinion_blocks,
                 *(body for _, body in footnote_pairs)):
        if frag is not None:
            _strip_noise_within_fragment(frag)

    out: list[str] = ["## Case Information", "", *meta, ""]

    if synopsis is not None:
        out += ["## Synopsis", "", _fragment_to_md(synopsis), ""]

    if headnotes is not None:
        out += ["## Headnotes", "", _fragment_to_md(headnotes), ""]

    for idx, opinion in enumerate(opinion_blocks, 1):
        title = "Opinion" if len(opinion_blocks) == 1 else f"Opinion {idx}"
        out += [f"## {title}", "", _fragment_to_md(opinion), ""]

    if footnote_pairs:
        fn_lines: list[str] = []
        for num, body in footnote_pairs:
            body_md = _fragment_to_md(body)
            if not body_md:
                continue
            body_one_line = re.sub(r"\s*\n\s*", " ", body_md).strip()
            prefix = f"[^{num}]: " if num else "- "
            fn_lines.append(prefix + body_one_line)
        if fn_lines:
            out += ["## Footnotes", "", *fn_lines, ""]

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip() + "\n"


def convert_clean_html_to_md(clean_html_path: Path, md_path: Path) -> None:
    """清洗后的 HTML → Markdown，写入 md_path。"""
    html = clean_html_path.read_text(encoding="utf-8")
    md_text = clean_html_to_md_text(html)
    md_path.write_text(md_text, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# 编排
# ══════════════════════════════════════════════════════════════════

def run_pipeline(
    raw_dir: Path = RAW_HTML_DIR,
    clean_dir: Path = CLEAN_HTML_DIR,
    md_dir: Path = MD_DIR,
    mode: str = "all",
    use_local_images: bool = False,
) -> None:
    """逐文件流式处理。

    mode:
        "all"   —— 清洗 + Markdown
        "clean" —— 仅清洗
        "md"    —— 仅 Markdown（从已有清洗产物读取）

    use_local_images:
        True  —— 尝试用 content_images/ 下的本地图替换 Westlaw 图床 URL
                  （clean 阶段有效；md 固定输出 blobid: 链接）
    """
    do_clean = mode in ("all", "clean")
    do_md = mode in ("all", "md")

    if do_clean:
        if not raw_dir.exists():
            raise SystemExit(f"源 HTML 目录不存在：{raw_dir}")
        clean_dir.mkdir(parents=True, exist_ok=True)
    if do_md:
        md_dir.mkdir(parents=True, exist_ok=True)

    image_index: dict = (
        load_local_image_index(Path.cwd()) if (do_clean and use_local_images) else {}
    )

    # 只跑 md 时从清洗目录扫文件；否则从原始目录
    source_dir = raw_dir if do_clean else clean_dir
    if not source_dir.exists():
        raise SystemExit(f"输入目录不存在：{source_dir}")

    pages: list[tuple[str, str]] = []
    total_replaced = 0

    for source_html in sorted(source_dir.glob("*.html")):
        if source_html.name == "index.html":
            continue

        clean_html = clean_dir / source_html.name

        if do_clean:
            title, replaced = clean_single_file(source_html, clean_html, image_index)
            pages.append((clean_html.name, title))
            total_replaced += replaced
            print(f"[清洗] {source_html.name} -> {clean_html.name} (local images: {replaced})")

        if do_md:
            if not clean_html.exists():
                print(f"[跳过] {clean_html.name} 不存在，先跑 --mode clean")
                continue
            md_path = md_dir / (clean_html.stem + ".md")
            convert_clean_html_to_md(clean_html, md_path)
            print(f"[MD]   {clean_html.name} -> {md_path.name}")

    if do_clean and pages:
        build_index(clean_dir, pages)
        print(f"[清洗] 目录页 index.html 已生成（共 {len(pages)} 条，本地图共用 {total_replaced}）")


def _resolve_cli_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


# ══════════════════════════════════════════════════════════════════
# DB 模式：从 MongoDB doc_items 读 doc_html，写 doc_html_clean / doc_md_clean
#
# 在 doc_items 文档上新增 / 维护的字段：
#   doc_html_clean             清洗后的离线可读 HTML 页面
#   doc_md_clean               转换后的 Markdown 文本
#   doc_md_clean_len           MD 字节长度
#   clean_process_status       null/0=待处理，2=成功，3=失败
#   clean_process_fail_reason  失败原因（截断 500 字）
#   clean_process_updated_at   最近一次清洗写入时间（UTC）
# ══════════════════════════════════════════════════════════════════

from datetime import datetime, timezone


def _db_collection():
    """懒加载：只有在 DB 模式下才 import pymongo / script.db。"""
    from script.db import get_doc_items_collection  # noqa: PLC0415
    return get_doc_items_collection()


def _resolve_key(key: str):
    """--id 接受 ObjectId 十六进制。"""
    from bson import ObjectId  # noqa: PLC0415
    from bson.errors import InvalidId  # noqa: PLC0415
    try:
        return {"_id": ObjectId(key)}
    except (InvalidId, TypeError):
        raise SystemExit(f"无效的 ObjectId 字符串：{key!r}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _process_html_to_outputs(html: str, doc_key: str) -> tuple[str, str]:
    """纯函数：doc_html → (doc_html_clean, doc_md_clean)。"""
    clean_page, _title = sanitize_html_string(html, doc_key=doc_key)
    md_text = clean_html_to_md_text(clean_page)
    return clean_page, md_text


def process_doc(col, query: dict) -> bool:
    """处理 query 匹配到的单条文档。成功返回 True，否则 False。"""
    doc = col.find_one(query, {"_id": 1, "doc_html": 1, "title": 1, "docGuid": 1})
    if doc is None:
        print(f"[跳过] {query} 不存在")
        return False

    _id = doc["_id"]
    doc_html = doc.get("doc_html")
    label = doc.get("docGuid") or str(_id)
    if not doc_html:
        col.update_one(
            {"_id": _id},
            {"$set": {
                "clean_process_status": 3,
                "clean_process_fail_reason": "doc_html is empty",
                "clean_process_updated_at": _utc_now(),
            }},
        )
        print(f"[失败] {label} doc_html 为空")
        return False

    try:
        clean_html, md_text = _process_html_to_outputs(
            doc_html, doc_key=doc.get("title") or label
        )
    except Exception as err:  # noqa: BLE001
        col.update_one(
            {"_id": _id},
            {"$set": {
                "clean_process_status": 3,
                "clean_process_fail_reason": f"{type(err).__name__}: {err}"[:500],
                "clean_process_updated_at": _utc_now(),
            }},
        )
        print(f"[失败] {label} {type(err).__name__}: {err}")
        return False

    col.update_one(
        {"_id": _id},
        {
            "$set": {
                "doc_html_clean": clean_html,
                "doc_md_clean": md_text,
                "doc_md_clean_len": len(md_text),
                "clean_process_status": 2,
                "clean_process_updated_at": _utc_now(),
            },
            "$unset": {"clean_process_fail_reason": ""},
        },
    )
    print(f"[OK  ] {label} md={len(md_text)}")
    return True


def process_batch(col, force: bool = False, limit: int | None = None) -> tuple[int, int]:
    """批量扫描。返回 (成功数, 失败数)。

    force=False：只挑 clean_process_status IN (null, 0, 3) 的文档
    force=True ：所有 doc_html 非空文档都重跑
    limit      ：上限，None=全部
    """
    base = {"doc_html": {"$ne": None, "$exists": True}}
    if not force:
        base["$or"] = [
            {"clean_process_status": {"$exists": False}},
            {"clean_process_status": {"$in": [None, 0, 3]}},
        ]

    cursor = col.find(base, {"_id": 1}).sort("_id", 1)
    if limit is not None:
        cursor = cursor.limit(limit)
    ids = [d["_id"] for d in cursor]

    print(f"[批次] 待处理 {len(ids)} 条（force={force}, limit={limit}）")
    ok = fail = 0
    for _id in ids:
        if process_doc(col, {"_id": _id}):
            ok += 1
        else:
            fail += 1
    print(f"[批次] 完成：成功 {ok} / 失败 {fail}")
    return ok, fail


def ensure_indexes(col) -> None:
    col.create_index("clean_process_status")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Westlaw HTML into cleaned offline HTML and Markdown.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("all", "clean", "md", "db-row", "db-batch"),
        default="all",
        help="all/clean/md 走文件系统；db-row/db-batch 读写 MongoDB doc_items",
    )
    # 文件系统模式参数
    parser.add_argument("--src", default=str(RAW_HTML_DIR), help="Raw HTML directory (fs mode)")
    parser.add_argument("--clean-out", default=str(CLEAN_HTML_DIR),
                        help="Cleaned offline HTML directory (fs mode)")
    parser.add_argument("--md-out", default=str(MD_DIR),
                        help="Markdown output directory (fs mode)")
    parser.add_argument("--use-local-images", action="store_true",
                        help="Replace Westlaw image URLs with local files (fs mode)")
    # DB 模式参数
    parser.add_argument("--id", help="ObjectId 十六进制 (db-row mode)")
    parser.add_argument("--guid", help="docGuid 业务键 (db-row mode)")
    parser.add_argument("--limit", type=int,
                        help="db-batch 最多处理多少条（默认全部）")
    parser.add_argument("--force", action="store_true",
                        help="重跑（忽略 clean_process_status）")
    args = parser.parse_args()

    if args.mode in ("db-row", "db-batch"):
        col = _db_collection()
        ensure_indexes(col)
        if args.mode == "db-row":
            if args.id:
                query = _resolve_key(args.id)
            elif args.guid:
                query = {"docGuid": args.guid}
            else:
                raise SystemExit("--mode db-row 必须指定 --id 或 --guid")
            process_doc(col, query)
        else:
            process_batch(col, force=args.force, limit=args.limit)
        return

    run_pipeline(
        raw_dir=_resolve_cli_path(args.src),
        clean_dir=_resolve_cli_path(args.clean_out),
        md_dir=_resolve_cli_path(args.md_out),
        mode=args.mode,
        use_local_images=args.use_local_images,
    )


if __name__ == "__main__":
    main()
