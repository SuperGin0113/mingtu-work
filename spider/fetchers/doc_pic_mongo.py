"""文档图片下载（Mongo 版）。
逻辑与 spider/fetchers/doc_pic.py（PG 版）一致：BrowserManager 兜底 CF/login，curl_cffi 拉图，
PNG magic bytes 校验。图片落本地文件，元数据进 mongo doc_images。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from script.project_paths import DATA_DIR
from spider.config import MAX_CONSECUTIVE_FAILS, PIC_REQUEST_INTERVAL
from spider.logging import setup_file_logger
from spider.login import ensure_logged_in
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

logger = setup_file_logger("fetch_pic_mongo", DATA_DIR / "fetch_pic_mongo.log")

IMAGE_DIR = DATA_DIR / "images"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _build_session():
    return make_session(
        accept="image/png,image/webp,image/*,*/*;q=0.8",
        referer=f"{BASE_URL}/",
        logger=logger,
    )


# ==================== 响应分类 ====================

def classify_image_response(status_code, content_bytes):
    if status_code in (401, 403):
        return "http_auth_error"
    if status_code == 429:
        return "rate_limited"
    if status_code in (404, 410, 451):
        return "not_found"
    if status_code >= 500:
        return f"server_error_{status_code}"
    if status_code != 200:
        return f"http_{status_code}"

    if not content_bytes or len(content_bytes) < 100:
        return "body_too_small"

    if content_bytes[:8] == PNG_MAGIC:
        return None

    text_head = content_bytes[:2000].decode("utf-8", errors="ignore").lower()
    if any(m in text_head for m in CF_MARKERS):
        return "cloudflare_challenge"
    if any(m in text_head for m in LOGIN_MARKERS):
        return "login_redirect"
    return "not_png"


# ==================== 请求 / 落盘 ====================

def fetch_image(session, url):
    resp = session.get(url, timeout=30, allow_redirects=True)
    return resp.status_code, resp.content


def save_image_file(case_document_guid, blob_id, content_bytes):
    """保存到本地 data/images/<case_document_guid>/<blob_id>.png，返回相对路径。"""
    save_dir = IMAGE_DIR / (case_document_guid or "_unknown")
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"{blob_id}.png"
    save_path.write_bytes(content_bytes)
    return f"data/images/{case_document_guid or '_unknown'}/{blob_id}.png"


# ==================== 工具命令 ====================

def print_status() -> None:
    summary = store.image_summary()
    total = sum(summary.values())
    logger.info(f"[status] doc_images total = {total}")
    for s in (STATUS_PENDING, STATUS_PROCESSING, STATUS_SUCCESS, STATUS_RETRYABLE, STATUS_FAILED):
        logger.info(f"  status={s} ({STATUS_LABELS[s]:>10}): {summary.get(s, 0)}")


def print_dry_run(limit: int) -> None:
    rows = store.get_pending_image_rows(limit=limit)
    if not rows:
        _empty_hint()
        return
    logger.info(f"[dry-run] would download {len(rows)} images:")
    for img_id, doc_id, case_guid, blob_id, image_url, retry in rows:
        logger.info(
            f"  doc={doc_id} case={case_guid} blob={(blob_id or '')[:20]}... retry={retry}"
        )


def reset_failed() -> None:
    n = store.reset_failed_images()
    logger.info(f"[reset] reset {n} failed doc_images → pending")


def _empty_hint() -> None:
    total = sum(store.image_summary().values())
    if total == 0:
        logger.info(
            "[done] doc_images 集合是空的；先跑 browse 把正文爬下来：\n"
            "  python -m spider.fetchers.browse --fetch-only --limit N\n"
            "（或全链路：python -m spider.fetchers.browse --url '<browse_url>' --limit N）"
        )
    else:
        logger.info("[done] no pending images")


# ==================== 主流程 ====================

def run(limit: int = 0):
    reset = store.reset_stale_image_processing()
    if reset:
        logger.info(f"[init] reset {reset} stale processing rows to pending")
    summary = store.image_summary()
    total = sum(summary.values())
    done = summary.get(STATUS_SUCCESS, 0)
    logger.info(f"[init] doc_images: {done}/{total} done")

    rows = store.get_pending_image_rows(limit=limit)
    if not rows:
        _empty_hint()
        return

    logger.info(f"[start] {len(rows)} images to download")
    bm = BrowserManager(logger=logger)
    session = _build_session()
    consecutive_fails = 0

    try:
        for i, (img_id, doc_item_id, case_guid, blob_id, image_url, retry_count) in enumerate(rows):
            logger.info(
                f"\n[{i+1}/{len(rows)}] img_id={img_id} doc={doc_item_id} "
                f"case={case_guid} blob={(blob_id or '')[:20]}... retries={retry_count}"
            )
            store.update_image_row(img_id, STATUS_PROCESSING)

            try:
                status_code, content = fetch_image(session, image_url)
                reason = classify_image_response(status_code, content)
                logger.info(f"  http={status_code} size={len(content):,} reason={reason}")

                if reason == "cloudflare_challenge":
                    logger.info("  [cf] using playwright to solve challenge...")
                    solved = bm.solve_challenge(image_url)
                    session = _build_session()
                    if solved:
                        time.sleep(random.uniform(5.0, 10.0))
                        status_code, content = fetch_image(session, image_url)
                        reason = classify_image_response(status_code, content)
                        logger.info(f"  [cf] retry http={status_code} size={len(content):,} reason={reason}")
                    if reason is None:
                        rel_path = save_image_file(case_guid, blob_id, content)
                        store.update_image_row(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, after cf) -> {rel_path}")
                        consecutive_fails = 0
                    else:
                        store.update_image_row(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason})")

                elif reason in ("login_redirect", "http_auth_error"):
                    logger.info(f"  [{reason}] using playwright to refresh login...")
                    bm.refresh_login()
                    session = _build_session()
                    time.sleep(random.uniform(3.0, 6.0))
                    status_code, content = fetch_image(session, image_url)
                    reason = classify_image_response(status_code, content)
                    logger.info(f"  [auth] retry http={status_code} size={len(content):,} reason={reason}")
                    if reason is None:
                        rel_path = save_image_file(case_guid, blob_id, content)
                        store.update_image_row(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                        consecutive_fails = 0
                    else:
                        store.update_image_row(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or "auth_failed")
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason or 'auth_failed'})")

                elif reason is None:
                    rel_path = save_image_file(case_guid, blob_id, content)
                    store.update_image_row(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                    logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}) -> {rel_path}")
                    consecutive_fails = 0

                elif reason == "not_found":
                    store.update_image_row(img_id, STATUS_FAILED, fail_reason=reason)
                    logger.info(f"  -> {STATUS_FAILED} ({STATUS_LABELS[STATUS_FAILED]}, {reason})")
                    consecutive_fails = 0

                elif reason == "rate_limited":
                    store.update_image_row(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason}), pausing 60s...")
                    time.sleep(60)

                else:
                    store.update_image_row(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason})")

            except Exception as e:
                err_reason = f"exception: {type(e).__name__}: {e}"
                logger.info(f"  error: {e}")
                store.update_image_row(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=err_reason)
                consecutive_fails += 1
                logger.info(f"  -> {STATUS_RETRYABLE} ({err_reason})")

            store.update_doc_image_status(doc_item_id)

            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                logger.info(f"\n[abort] {consecutive_fails} consecutive failures, stopping")
                break

            if i < len(rows) - 1:
                delay = random.uniform(*PIC_REQUEST_INTERVAL)
                logger.info(f"  [wait] {delay:.1f}s")
                time.sleep(delay)

    finally:
        logger.info("\n[summary]")
        for s, c in sorted(store.image_summary().items()):
            logger.info(f"  status={s} ({STATUS_LABELS.get(s, '?')}): {c}")
        bm.close()


def main():
    parser = argparse.ArgumentParser(description="下载 doc_images (mongo) 中待处理的图片")
    parser.add_argument("--limit", type=int, default=0, help="最多下载几张；0=全部 (默认: 0)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="仅打印当前 status 分布并退出")
    g.add_argument("--dry-run", action="store_true", help="只列出会被下载的图片，不发请求")
    g.add_argument("--reset-failed", action="store_true",
                   help="把 status=FAILED 的全部重置回 PENDING（清 retry_count + fail_reason）")
    args = parser.parse_args()

    if args.status:
        print_status()
        return
    if args.reset_failed:
        reset_failed()
        return
    if args.dry_run:
        print_dry_run(limit=args.limit)
        return

    ensure_logged_in()
    run(limit=args.limit)


if __name__ == "__main__":
    main()
