"""Westlaw spider 主入口：从 Browse URL 出发，采列表 + 爬正文 HTML，落 MongoDB。

两阶段：
    1. browse   ：playwright 打开 --url，监听 /Search/v1/results 的 XHR，提取
                  listItems[] → upsert mongo doc_items（带 docUrl）
    2. fetch    ：从 mongo 取 pending（按 caseDocumentGuid 自动去重），用
                  curl_cffi 拉每条 docUrl 的 HTML，写回 doc_html 字段。
                  CF challenge / 登录失效时由共享 BrowserManager 兜底。

默认两步串跑；可用 --browse-only 只采列表、--fetch-only 只爬正文（断点续跑 / 重试）。
图片下载是独立第三阶段：python -m spider.fetchers.doc_pic_mongo --limit N
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from playwright.sync_api import sync_playwright

from script.project_paths import DATA_DIR
from spider.config import HTML_REQUEST_INTERVAL, MAX_CONSECUTIVE_FAILS
from spider.logging import setup_file_logger
from spider.login import ensure_logged_in
from spider.login.browser_login import get_authenticated_context
from spider.login.browser_manager import BASE_URL, CF_MARKERS, LOGIN_MARKERS, BrowserManager
from spider.session import make_session
from spider.store import mongo as store
from spider.store.mongo import (
    STATUS_FAILED,
    STATUS_LABELS,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_RETRYABLE,
    STATUS_SUCCESS,
)

logger = setup_file_logger("spider.browse", DATA_DIR / "browse.log")

# 翻页/加载更多按钮：从最具体到最宽泛
# ⚠️ 不要用 `[aria-label*='Next' i]` 这种不限定容器的 selector：Westlaw 页面里
#   doc preview 区另有一颗 <button aria-label="Next">(切下一篇文档),会被误点,
#   表现是 +100 listItem 但 docGuid 重复,upsert 不增、爬完只剩第一页。
#   分页用的真 Next 在 .co_paginationBlockPart 这个容器里,id 是
#   #co_search_header_pagination_next(<a> 标签,href 带 startIndex=101)。
NEXT_BUTTON_SELECTORS = (
    "a#co_search_header_pagination_next:not([aria-disabled='true'])",
    "button#co_search_results_nextPage",
    "a#co_search_results_nextPage",
    "button.co_searchResultsPagingControlNext",
    "a.co_searchResultsPagingControlNext",
    ".co_paginationBlockPart a[aria-label*='Next' i]:not([aria-disabled='true'])",
    ".co_paginationBlockPart button[aria-label*='Next' i]:not([disabled])",
    ".co_paginationBlockPart a:has-text('Next')",
    "button:has-text('Show more'):not([disabled])",
    "button:has-text('Load more'):not([disabled])",
)


# ==================== URL / listItem 解析 ====================

def _iter_list_items(payload: dict):
    for section in payload.get("resultSections") or []:
        for item in section.get("listItems") or []:
            yield item


def _extract_guid_from_url(browse_url: str) -> str | None:
    try:
        qs = parse_qs(urlparse(browse_url).query)
        v = qs.get("guid")
        return v[0] if v else None
    except Exception:
        return None


# `categoryPageTitle` 形如 "k2056 —In general" / "K2056 -In general"
_KEY_NUMBER_FROM_TITLE = re.compile(r"^\s*([kK]\d+)\b")


def _extract_search_key_number(xhr_url: str) -> str | None:
    """从 /Search/v1/results XHR URL 的 resultPageData.categoryPageTitle 抽 k段(如 k2056)。"""
    try:
        rpd = parse_qs(urlparse(xhr_url).query).get("resultPageData")
        if not rpd:
            return None
        title = (json.loads(rpd[0]) or {}).get("categoryPageTitle") or ""
        m = _KEY_NUMBER_FROM_TITLE.match(title)
        return m.group(1).lower() if m else None
    except Exception:
        return None


def _enrich_doc_url(item: dict) -> dict:
    record = dict(item)
    doc_link = item.get("docLink")
    if isinstance(doc_link, str) and doc_link.startswith("/"):
        record["docUrl"] = BASE_URL + doc_link
    return record


# ==================== 列表采集（playwright + XHR） ====================

class _Harvester:
    """监听 /Search/v1/results 响应，去重累积 listItems。"""

    def __init__(self, source_browse_url: str):
        self.source_browse_url = source_browse_url
        self.records: list[dict] = []
        self.seen_keys: set[tuple] = set()
        self.seen_xhr_urls: set[str] = set()
        self.search_key_number: str | None = None
        self._last_change = time.time()

    def on_response(self, response):
        try:
            url = response.url
            if "/Search/v1/results" not in url:
                return
            ctype = (response.headers or {}).get("content-type", "")
            if response.status != 200 or "json" not in ctype.lower():
                logger.info(
                    f"[xhr] /Search/v1/results dropped: status={response.status} ct={ctype}"
                )
                return
            if url in self.seen_xhr_urls:
                return
            self.seen_xhr_urls.add(url)
            if self.search_key_number is None:
                key = _extract_search_key_number(url)
                if key:
                    self.search_key_number = key
                    logger.info(f"[xhr] search_key_number = {key}")
            try:
                payload = response.json()
            except Exception:
                text = response.text() or ""
                try:
                    payload = json.loads(text)
                except Exception:
                    return
            added = 0
            for item in _iter_list_items(payload):
                key = (item.get("rank"), item.get("docLink"))
                if key in self.seen_keys:
                    continue
                self.seen_keys.add(key)
                self.records.append(_enrich_doc_url(item))
                added += 1
            if added:
                self._last_change = time.time()
                logger.info(f"[xhr] +{added} (total={len(self.records)}) from {url[:120]}")
        except Exception as e:
            logger.warning(f"[xhr] handler error: {e}")

    def settled_for(self, seconds: float) -> bool:
        return (time.time() - self._last_change) >= seconds


def _wait_through_cf(page, max_wait: float = 60.0) -> bool:
    """检测页面是否处于 CF challenge，循环等到通过或超时。"""
    deadline = time.time() + max_wait
    last_log = 0.0
    while time.time() < deadline:
        try:
            head = (page.content()[:5000] or "").lower()
        except Exception:
            head = ""
        if not any(m in head for m in CF_MARKERS):
            return True
        now = time.time()
        if now - last_log >= 10:
            logger.info("[browse] CF challenge detected, waiting...")
            last_log = now
        time.sleep(2)
    logger.info("[browse] CF challenge wait timed out")
    return False


def _click_next(page) -> bool:
    """尝试点 Next / Show More 按钮；点中返回 True。"""
    for sel in NEXT_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            loc.click(timeout=5000)
            logger.info(f"[browse] clicked next via selector: {sel}")
            return True
        except Exception:
            continue
    return False


def harvest(
    browse_url: str,
    limit: int,
    harvest_timeout: float = 120.0,
    settle_seconds: float = 8.0,
) -> tuple[list[dict], str | None]:
    """打开 browse_url，监听 XHR，累积 listItems。

    停止条件（任一满足）：达到 limit / 超时 / 没有 Next 按钮且滚动 ≥3 次仍无新 XHR。

    返回 (listItems, search_key_number);后者从 XHR `resultPageData.categoryPageTitle` 抽出。
    """
    h = _Harvester(browse_url)

    with sync_playwright() as pw:
        browser, context, page = get_authenticated_context(pw)
        context.on("response", h.on_response)
        try:
            logger.info(f"[browse] navigate -> {browse_url[:160]}")
            page.goto(browse_url, wait_until="domcontentloaded", timeout=60_000)
            _wait_through_cf(page, max_wait=60.0)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            try:
                logger.info(
                    f"[browse] landed url={page.url[:160]}  title={(page.title() or '')[:80]}"
                )
            except Exception:
                pass

            start = time.time()
            scroll_attempts_since_change = 0
            while True:
                if limit and len(h.records) >= limit:
                    logger.info(f"[browse] reached limit ({limit}), stop")
                    break
                if time.time() - start > harvest_timeout:
                    logger.info(f"[browse] timeout {harvest_timeout}s, stop")
                    break
                if h.settled_for(settle_seconds) and scroll_attempts_since_change >= 3:
                    logger.info(
                        f"[browse] no new XHR for {settle_seconds}s "
                        f"after {scroll_attempts_since_change} scrolls, stop"
                    )
                    break

                time.sleep(random.uniform(3.0, 7.0))
                _wait_through_cf(page, max_wait=30.0)

                before = len(h.records)
                if _click_next(page):
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                else:
                    try:
                        page.mouse.wheel(0, random.randint(800, 1600))
                    except Exception:
                        try:
                            page.evaluate("window.scrollBy(0, 1200)")
                        except Exception:
                            pass

                if len(h.records) > before:
                    scroll_attempts_since_change = 0
                else:
                    scroll_attempts_since_change += 1
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    if limit:
        h.records.sort(key=lambda r: r.get("rank") if isinstance(r.get("rank"), int) else 10**9)
        return h.records[:limit], h.search_key_number
    return h.records, h.search_key_number


# ==================== HTML 处理 ====================

def _build_session():
    return make_session(
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        referer=f"{BASE_URL}/Search/Home.html",
        logger=logger,
    )


def classify_response(result):
    status_code = result.get("status", 0)
    body_lower = (result.get("body") or "")[:5000].lower()

    if any(m in body_lower for m in CF_MARKERS):
        return "cloudflare_challenge"
    if any(m in body_lower for m in LOGIN_MARKERS):
        return "login_redirect"

    if status_code in (401, 403):
        return "http_auth_error"
    if status_code == 429:
        return "rate_limited"
    if status_code in (404, 410, 451):
        return "not_found"
    if status_code >= 500:
        return f"server_error_{status_code}"
    if status_code == 200 and len(result.get("body", "")) <= 500:
        return "body_too_small"
    return None


def _normalize_html(raw_html):
    raw_html = raw_html.replace('\r\n', '\n').replace('\r', '\n')
    raw_html = re.sub(r'\n{3,}', '\n\n', raw_html)
    return raw_html.strip()


_LINK_PATTERN = re.compile(
    r'<a\s[^>]*href="([^"]*Link/Document/Blob/([^.]+)\.png[^"]*)"[^>]*>'
    r'\s*<img\s([^>]*)/>',
    re.DOTALL,
)
_ATTR_PATTERN = re.compile(r'(\w+)="([^"]*)"')


def extract_content_images(doc_html):
    """从 HTML 中提取 content images（仅 png，按 blob_id 去重，取全尺寸 URL）。"""
    seen = {}
    position = 0
    for m in _LINK_PATTERN.finditer(doc_html):
        full_url = html_mod.unescape(m.group(1))
        blob_id = m.group(2)
        attrs = dict(_ATTR_PATTERN.findall(m.group(3)))
        alt_text = html_mod.unescape(attrs.get("alt", ""))
        w = attrs.get("width", "").replace("px", "")
        h = attrs.get("height", "").replace("px", "")
        width = int(w) if w.isdigit() else None
        height = int(h) if h.isdigit() else None

        if blob_id not in seen:
            seen[blob_id] = {
                "blob_id": blob_id,
                "image_url": full_url,
                "alt_text": alt_text,
                "width": width,
                "height": height,
                "position": position,
            }
            position += 1
        else:
            existing = seen[blob_id]
            if "maxHeight" not in full_url and "maxHeight" in existing["image_url"]:
                existing["image_url"] = full_url
            if width and (existing["width"] is None or width > existing["width"]):
                existing["width"] = width
                existing["height"] = height
    return list(seen.values())


def fetch_doc(session, url):
    resp = session.get(url, timeout=60, allow_redirects=True)
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": _normalize_html(resp.text),
    }


# ==================== 正文爬取（fetch HTML 阶段） ====================

def fetch_pending_html(limit: int = 0):
    """从 mongo 取 pending（按 caseDocumentGuid 去重）→ 逐条 curl_cffi 拉 HTML → 落 mongo。"""
    reset = store.reset_stale_doc_processing()
    if reset:
        logger.info(f"[init] reset {reset} stale processing rows to pending")
    summary = store.doc_summary()
    total = sum(summary.values())
    done = summary.get(STATUS_SUCCESS, 0)
    logger.info(f"[init] doc_items: {done}/{total} done")

    rows = store.get_pending_doc_rows(limit=limit)
    if not rows:
        _empty_hint()
        return

    bm = BrowserManager(logger=logger)  # lazy
    session = _build_session()
    consecutive_fails = 0
    refreshed_for_500 = False  # 500 触发刷新登录的"一次性令牌"，遇 SUCCESS 重置

    try:
        for i, (row_id, rank, doc_url, guid, retry_count) in enumerate(rows):
            logger.info(
                f"\n[{i+1}/{len(rows)}] rank={rank} id={row_id} guid={guid} retries={retry_count}"
            )
            logger.info(f"  url={(doc_url or '')[:120]}...")

            store.update_doc_row(row_id, STATUS_PROCESSING)

            try:
                result = fetch_doc(session, doc_url)
                status_code = result["status"]
                body_len = len(result["body"])
                reason = classify_response(result)
                logger.info(f"  http={status_code} len={body_len} reason={reason}")

                if reason == "cloudflare_challenge":
                    logger.info("  [cf] using playwright to solve challenge...")
                    html = bm.solve_challenge(doc_url)
                    session = _build_session()
                    if html and len(html) > 1000:
                        store.update_doc_row(row_id, STATUS_SUCCESS, doc_html=html)
                        store.save_content_images(row_id, extract_content_images(html))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, solved by playwright)")
                        consecutive_fails = 0
                        refreshed_for_500 = False
                    else:
                        time.sleep(random.uniform(5.0, 10.0))
                        result = fetch_doc(session, doc_url)
                        reason2 = classify_response(result)
                        if reason2 is None and result["status"] == 200 and len(result["body"]) > 500:
                            store.update_doc_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                            store.save_content_images(row_id, extract_content_images(result["body"]))
                            logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, retry after cf)")
                            consecutive_fails = 0
                            refreshed_for_500 = False
                        else:
                            store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason="cloudflare_unsolved")
                            consecutive_fails += 1
                            logger.info(f"  -> {STATUS_RETRYABLE} (cloudflare_unsolved)")

                elif reason in ("login_redirect", "http_auth_error"):
                    logger.info(f"  [{reason}] using playwright to refresh login...")
                    bm.refresh_login()
                    session = _build_session()
                    time.sleep(random.uniform(3.0, 6.0))
                    result = fetch_doc(session, doc_url)
                    status_code = result["status"]
                    body_len = len(result["body"])
                    reason = classify_response(result)
                    logger.info(f"  [auth] retry http={status_code} len={body_len} reason={reason}")
                    if reason is None and status_code == 200 and body_len > 500:
                        store.update_doc_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                        store.save_content_images(row_id, extract_content_images(result["body"]))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                        consecutive_fails = 0
                        refreshed_for_500 = False
                    else:
                        store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or "auth_failed")
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason or 'auth_failed'})")

                elif reason is None and status_code == 200 and body_len > 500:
                    store.update_doc_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                    store.save_content_images(row_id, extract_content_images(result["body"]))
                    logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                    consecutive_fails = 0
                    refreshed_for_500 = False

                elif reason == "not_found":
                    store.update_doc_row(row_id, STATUS_FAILED, fail_reason=reason)
                    logger.info(f"  -> {STATUS_FAILED} ({STATUS_LABELS[STATUS_FAILED]}, {reason})")
                    consecutive_fails = 0

                elif reason and reason.startswith("server_error_"):
                    # Westlaw 500 可能是 (a) 文档稳定坏 (b) cookie 半失效引发
                    # 首次撞 500 时刷新登录重试一次：是 cookie 问题就能恢复；不是则确认文档侧问题
                    if not refreshed_for_500:
                        logger.info(f"  [{reason}] first 500, try refresh login as defensive recovery...")
                        bm.refresh_login()
                        session = _build_session()
                        refreshed_for_500 = True
                        time.sleep(random.uniform(3.0, 6.0))
                        result = fetch_doc(session, doc_url)
                        status_code = result["status"]
                        body_len = len(result["body"])
                        reason = classify_response(result)
                        logger.info(f"  [500-retry] http={status_code} len={body_len} reason={reason}")
                        if reason is None and status_code == 200 and body_len > 500:
                            store.update_doc_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                            store.save_content_images(row_id, extract_content_images(result["body"]))
                            logger.info(f"  -> {STATUS_SUCCESS} (recovered after refresh)")
                            consecutive_fails = 0
                            refreshed_for_500 = False
                        else:
                            store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True,
                                                 fail_reason=reason or "server_error_persistent")
                            logger.info(f"  -> {STATUS_RETRYABLE} ({reason or 'persistent_500'}), not counted toward abort")
                    else:
                        store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason}), already refreshed, not counted toward abort")

                elif reason == "rate_limited":
                    store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason}), pausing 60s...")
                    time.sleep(60)

                else:
                    store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or f"http_{status_code}")
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason or f'http_{status_code}'})")

            except Exception as e:
                err_reason = f"exception: {type(e).__name__}: {e}"
                logger.info(f"  error: {e}")
                store.update_doc_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=err_reason)
                consecutive_fails += 1
                logger.info(f"  -> {STATUS_RETRYABLE} ({err_reason})")

            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                logger.info(f"\n[abort] {consecutive_fails} consecutive failures, stopping")
                break

            if i < len(rows) - 1:
                delay = random.uniform(*HTML_REQUEST_INTERVAL)
                logger.info(f"  [wait] {delay:.1f}s")
                time.sleep(delay)

    finally:
        logger.info("\n[summary]")
        for s, c in sorted(store.doc_summary().items()):
            logger.info(f"  status={s} ({STATUS_LABELS.get(s, '?')}): {c}")
        bm.close()


# ==================== 工具命令 ====================

def print_status() -> None:
    summary = store.doc_summary()
    total = sum(summary.values())
    logger.info(f"[status] doc_items total = {total}")
    for s in (STATUS_PENDING, STATUS_PROCESSING, STATUS_SUCCESS, STATUS_RETRYABLE, STATUS_FAILED):
        logger.info(f"  status={s} ({STATUS_LABELS[s]:>10}): {summary.get(s, 0)}")


def print_dry_run(limit: int) -> None:
    rows = store.get_pending_doc_rows(limit=limit)
    if not rows:
        _empty_hint()
        return
    logger.info(f"[dry-run] would fetch {len(rows)} rows from mongo:")
    for row_id, rank, doc_url, guid, retry in rows:
        logger.info(
            f"  rank={rank}  retry={retry}  caseGuid={guid}  url={(doc_url or '')[:100]}..."
        )


def print_dry_run_browse(url: str, limit: int, harvest_timeout: float) -> None:
    """采列表但不写 mongo，把会被入库的 listItems / docUrl 直接打印出来。"""
    logger.info("[dry-run] harvesting listItems (will NOT be written to mongo)")
    items, search_key_number = harvest(url, limit=limit, harvest_timeout=harvest_timeout)
    if not items:
        logger.warning("[dry-run] harvest returned 0 items")
        return
    logger.info(
        f"[dry-run] harvested {len(items)} listItems "
        f"(search_key_number={search_key_number})"
    )
    for item in items:
        logger.info(
            f"  rank={item.get('rank')}  caseGuid={item.get('caseDocumentGuid')}  "
            f"url={(item.get('docUrl') or '')[:100]}..."
        )


def reset_failed() -> None:
    n = store.reset_failed_docs()
    logger.info(f"[reset] reset {n} failed doc_items → pending")


def _empty_hint() -> None:
    total = sum(store.doc_summary().values())
    if total == 0:
        logger.info(
            "[done] doc_items 集合是空的；先采列表（不要带 --fetch-only）：\n"
            "  python -m spider.fetchers.browse --url '<browse_url>' --limit N --browse-only"
        )
    else:
        logger.info("[done] no pending rows")


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Westlaw spider：默认 browse(--url) 采列表 → 冷却 → 爬正文 HTML 落 mongo。"
            " 用 --browse-only / --fetch-only 控制只跑某一阶段。"
        )
    )
    parser.add_argument("--url", help="Browse/Home/WestKeyNumberSystem 入口 URL（采列表阶段必需）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几条；0=不限（默认: 0）")
    parser.add_argument(
        "--harvest-timeout", type=float, default=120.0, help="browse 阶段等 XHR 的最长秒数（默认: 120）"
    )
    parser.add_argument(
        "--cool-down", type=float, default=-1.0,
        help="browse → fetch 间冷却秒数；<0 = 随机 60-120s（默认）",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browse-only", action="store_true", help="只采列表，不爬正文")
    mode.add_argument("--fetch-only", action="store_true",
                      help="跳过采列表，只爬已收集的 pending 文档（断点续跑）")
    mode.add_argument("--status", action="store_true", help="仅打印当前 status 分布并退出")
    mode.add_argument("--dry-run", action="store_true",
                      help="预览：带 --url 时做一次 harvest 不写库；不带 --url 时列 mongo 中 pending")
    mode.add_argument("--reset-failed", action="store_true",
                      help="把 status=FAILED 的全部重置回 PENDING（清 retry_count + fail_reason）")
    args = parser.parse_args()

    if args.status:
        print_status()
        return
    if args.reset_failed:
        reset_failed()
        return
    if args.dry_run:
        if args.url:
            ensure_logged_in()
            print_dry_run_browse(
                args.url,
                limit=args.limit,
                harvest_timeout=args.harvest_timeout,
            )
        else:
            print_dry_run(limit=args.limit)
        return

    if args.fetch_only:
        ensure_logged_in()
        fetch_pending_html(limit=args.limit)
        return

    if not args.url:
        parser.error(
            "--url 必填（除非用 --fetch-only / --status / --dry-run / --reset-failed）"
        )

    ensure_logged_in()
    store.ensure_indexes()
    source_browse_guid = _extract_guid_from_url(args.url)
    logger.info(f"[browse] source_browse_guid = {source_browse_guid}")

    items, search_key_number = harvest(
        args.url, limit=args.limit, harvest_timeout=args.harvest_timeout
    )
    logger.info(
        f"[harvest] total {len(items)} listItems collected "
        f"(search_key_number={search_key_number})"
    )

    if not items:
        logger.warning("[harvest] empty result, aborting")
        return

    res = store.upsert_list_items(
        items,
        source_browse_url=args.url,
        source_browse_guid=source_browse_guid,
        search_key_number=search_key_number,
    )
    logger.info(f"[mongo] upserted={res['upserted']}  modified={res['modified']}")

    if args.browse_only:
        logger.info("[done] --browse-only, skipping fetch stage")
        return

    cool = args.cool_down if args.cool_down >= 0 else random.uniform(10.0, 40.0)
    if cool > 0:
        logger.info(f"[cool] sleeping {cool:.1f}s before fetch stage")
        time.sleep(cool)

    fetch_pending_html(limit=args.limit)


if __name__ == "__main__":
    main()
