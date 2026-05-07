"""
从 doc_images 表取待下载记录，用缓存的 cookie 下载图片到本地。
只处理 is_primary 文档的图片。
状态机：0 待下载 -> 1 下载中 -> 2 成功 / 3 可重试 / 4 不可重试

架构：
- 启动时打开 playwright 浏览器并保持，用于登录刷新 cookie 和处理人机验证
- 数据请求用 curl_cffi（更快、更轻量）
- 遇到 CF 验证或登录过期时，用 playwright 浏览器处理后提取新 cookie
- 下载前校验响应是否为真实图片（PNG magic bytes），防止登录页伪装
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import psycopg2
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

try:
    import oss2
except ImportError:
    oss2 = None

from script.db import DB_CONFIG, DB_NAME
from script.project_paths import DATA_DIR, ENV_FILE
from spider.login.browser_login import COOKIE_TTL, get_authenticated_context
from spider.paths import COOKIE_FILE, STORAGE_FILE

# 从 .env 加载环境变量
load_dotenv(ENV_FILE)

# 日志配置：终端 + 文件同时输出
LOG_DIR = DATA_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "fetch_pic.log"

logger = logging.getLogger("fetch_pic")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

BASE_URL = "https://1.next.westlaw.com"
IMAGE_DIR = DATA_DIR / "images"
LOCAL_MIRROR_DIR = DATA_DIR / "pictmp"
REQUEST_INTERVAL = (8.0, 17.0)

# OSS 配置（从环境变量读取，未设置则退回本地存储）
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "oss-cn-beijing.aliyuncs.com")
OSS_BUCKET_NAME = os.getenv("OSS_BUCKET_NAME", "westpatent")
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_KEY_PREFIX = os.getenv("OSS_KEY_PREFIX", "westlaw/images")

# PNG 文件头 magic bytes
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# status 状态码
STATUS_PENDING = 0       # 待下载
STATUS_PROCESSING = 1    # 下载中
STATUS_SUCCESS = 2       # 下载成功
STATUS_RETRYABLE = 3     # 下载失败（可重试）
STATUS_FAILED = 4        # 下载失败（不可重试）

STATUS_LABELS = {
    STATUS_PENDING: "pending",
    STATUS_PROCESSING: "processing",
    STATUS_SUCCESS: "success",
    STATUS_RETRYABLE: "retryable",
    STATUS_FAILED: "failed",
}

MAX_RETRIES = 3              # 单条记录最大重试次数，超过转 status=4
MAX_CONSECUTIVE_FAILS = 3    # 连续失败次数上限，触发后中断本轮

ALLOWED_COOKIE_DOMAINS = (
    ".westlaw.com", "westlaw.com",
    ".next.westlaw.com", "next.westlaw.com",
    "1.next.westlaw.com",
    ".1.next.westlaw.com",
    ".i1.next.westlaw.com",
    ".c1.next.westlaw.com",
)


# ==================== Playwright 浏览器管理 ====================

class BrowserManager:
    """管理一个常驻的 playwright 浏览器实例，用于登录和处理人机验证。"""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        """启动浏览器，完成登录，提取 cookie。"""
        logger.info("[browser] starting playwright browser...")
        self.pw = sync_playwright().start()
        self.browser, self.context, self.page = get_authenticated_context(self.pw)
        self._save_cookies()
        logger.info("[browser] browser ready and kept alive")

    def _save_cookies(self):
        """从浏览器提取 cookie 保存到文件。"""
        cookies = self.context.cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        self.context.storage_state(path=str(STORAGE_FILE))
        logger.info(f"[browser] cookies saved ({len(cookies)} cookies)")

    def solve_challenge(self, url):
        """
        用 playwright 访问触发了 CF 验证的 URL，等待验证通过，提取新 cookie。
        """
        logger.info(f"[browser] navigating to solve challenge: {url[:100]}...")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

            for attempt in range(30):
                time.sleep(3)
                title = self.page.title() or ""
                content = self.page.content()
                content_lower = content[:5000].lower()

                has_challenge = any(m in content_lower for m in (
                    "cf-browser-verification", "challenge-platform",
                    "just a moment", "checking your browser",
                ))

                if not has_challenge and len(content) > 1000:
                    logger.info(f"[browser] challenge solved after {(attempt+1)*3}s, title: {title}")
                    self._save_cookies()
                    return True

                if attempt % 5 == 4:
                    logger.info(f"[browser] still waiting for challenge... ({(attempt+1)*3}s)")

            logger.info("[browser] challenge timeout after 90s")
            self._save_cookies()
            return False

        except Exception as e:
            logger.info(f"[browser] solve_challenge error: {e}")
            self._save_cookies()
            return False

    def refresh_login(self):
        """重新登录（cookie 过期时调用）。"""
        logger.info("[browser] refreshing login...")
        try:
            self.page.goto(
                f"{BASE_URL}/Search/Home.html?transitionType=Default&contextData=(sc.Default)",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(3)

            content_lower = self.page.content()[:5000].lower()
            if "signon" in content_lower or "loginform" in content_lower:
                self.close()
                self.start()
            else:
                self._save_cookies()
                logger.info("[browser] login still valid, cookies refreshed")
        except Exception as e:
            logger.info(f"[browser] refresh_login error: {e}, restarting...")
            self.close()
            self.start()

    def close(self):
        """关闭浏览器。"""
        for obj in (self.context, self.browser):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass
        self.pw = self.browser = self.context = self.page = None
        logger.info("[browser] closed")


# ==================== Cookie / Session ====================

def make_session():
    with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
        cookies = json.load(f)
    session = cffi_requests.Session(impersonate="chrome120")
    injected = 0
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        if not any(domain == d or domain.endswith(d) for d in ALLOWED_COOKIE_DOMAINS):
            continue
        try:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
            injected += 1
        except Exception:
            pass
    logger.info(f"[session] injected {injected}/{len(cookies)} cookies")

    session.headers.update({
        "Accept": "image/png,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    return session


# ==================== 响应分类 ====================

def classify_image_response(status_code, content_bytes):
    """
    判断图片下载响应类型。
    返回失败原因字符串，正常则返回 None。
    """
    # 先检查 HTTP 状态码
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

    # 200 但内容不是 PNG -> 可能是登录页/CF 页
    if not content_bytes or len(content_bytes) < 100:
        return "body_too_small"

    if content_bytes[:8] == PNG_MAGIC:
        return None  # 真正的 PNG

    # 不是 PNG，检查是不是 HTML（登录页/CF 验证页）
    text_head = content_bytes[:2000].decode("utf-8", errors="ignore").lower()

    for marker in ("cf-browser-verification", "challenge-platform", "cf-chl-bypass",
                    "just a moment", "checking your browser"):
        if marker in text_head:
            return "cloudflare_challenge"

    for marker in ("signon.thomsonreuters", "cosi/signon", "sessionexpired",
                    "please sign in", "your session has expired", "loginform",
                    "productid=cbt", "redirectto"):
        if marker in text_head:
            return "login_redirect"

    return "not_png"


# ==================== DB ====================

def ensure_schema():
    """确保 doc_images 表有 retry_count/fail_reason 列，恢复中断状态。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE doc_images ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE doc_images ADD COLUMN IF NOT EXISTS fail_reason TEXT")

        # 统计
        cur.execute("""
            SELECT count(*) FROM doc_images di
            JOIN doc_items d ON d.id = di.doc_item_id
            WHERE d.is_primary = TRUE
        """)
        total = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM doc_images di
            JOIN doc_items d ON d.id = di.doc_item_id
            WHERE d.is_primary = TRUE AND di.status = %s
        """, (STATUS_SUCCESS,))
        done = cur.fetchone()[0]
        logger.info(f"[init] doc_images: {done}/{total} primary images done")

        # 把上次中断卡在 processing 的记录重置为 pending
        cur.execute("""
            UPDATE doc_images SET status = %s
            WHERE status = %s
              AND doc_item_id IN (SELECT id FROM doc_items WHERE is_primary = TRUE)
        """, (STATUS_PENDING, STATUS_PROCESSING))
        if cur.rowcount:
            logger.info(f"[init] reset {cur.rowcount} stale processing rows to pending")
    conn.close()


def get_pending_images(limit=0):
    """取 is_primary 文档的待下载(0) + 可重试(3) 图片记录，排除已超过重试上限的。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    with conn.cursor() as cur:
        sql = """
            SELECT di.id, di.doc_item_id, d.case_document_guid, di.blob_id, di.image_url, di.retry_count
            FROM doc_images di
            JOIN doc_items d ON d.id = di.doc_item_id
            WHERE d.is_primary = TRUE
              AND di.status IN (%s, %s)
              AND di.retry_count < %s
            ORDER BY di.doc_item_id, di.position
        """
        if limit > 0:
            sql += f" LIMIT {limit}"
        cur.execute(sql, (STATUS_PENDING, STATUS_RETRYABLE, MAX_RETRIES))
        rows = cur.fetchall()

        # 超过重试上限的直接转 FAILED
        cur.execute("""
            UPDATE doc_images SET status = %s
            WHERE status = %s AND retry_count >= %s
              AND doc_item_id IN (SELECT id FROM doc_items WHERE is_primary = TRUE)
        """, (STATUS_FAILED, STATUS_RETRYABLE, MAX_RETRIES))
        if cur.rowcount:
            logger.info(f"[init] {cur.rowcount} images exceeded max retries({MAX_RETRIES}), marked as failed")
    conn.commit()
    conn.close()
    return rows


def update_image(img_id, status, file_path=None, file_size=None, inc_retry=False, fail_reason=None):
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        if file_path is not None:
            cur.execute("""
                UPDATE doc_images
                SET status = %s, oss_key = %s, file_size = %s,
                    fail_reason = NULL, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (status, file_path, file_size, img_id))
        elif inc_retry:
            cur.execute("""
                UPDATE doc_images
                SET status = %s, retry_count = retry_count + 1,
                    fail_reason = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (status, fail_reason, img_id))
        else:
            cur.execute("""
                UPDATE doc_images
                SET status = %s, fail_reason = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (status, fail_reason, img_id))
    conn.close()


def update_doc_image_status(doc_item_id):
    """根据子表状态更新主表 image_status。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), count(*) FILTER (WHERE status = 2),
                   count(*) FILTER (WHERE status IN (3, 4))
            FROM doc_images WHERE doc_item_id = %s
        """, (doc_item_id,))
        total, success, failed = cur.fetchone()
        if total == 0:
            return
        if success == total:
            image_status = STATUS_SUCCESS
        elif failed > 0 and success > 0:
            image_status = STATUS_RETRYABLE  # 部分失败
        elif failed == total:
            image_status = STATUS_FAILED
        else:
            return  # 还有 pending/processing 的，不更新
        cur.execute(
            "UPDATE doc_items SET image_status = %s WHERE id = %s",
            (image_status, doc_item_id),
        )
    conn.close()


# ==================== 请求 ====================

def fetch_image(session, url):
    """下载图片，返回 (status_code, content_bytes)。"""
    resp = session.get(url, timeout=30, allow_redirects=True)
    return resp.status_code, resp.content


def save_image_file(case_document_guid, blob_id, content_bytes, oss_bucket=None):
    """
    保存图片：若传入 oss_bucket 则上传到 OSS，否则存本地。
    路径按 case_document_guid 分目录（同一案件的多条 headnote 共用）。
    返回 oss_key 或本地相对路径。
    """
    if oss_bucket is not None:
        oss_key = f"{OSS_KEY_PREFIX}/{case_document_guid}/{blob_id}.png"
        result = oss_bucket.put_object(oss_key, content_bytes)
        if result.status != 200:
            raise RuntimeError(f"OSS upload failed: status={result.status}")
        try:
            mirror_dir = LOCAL_MIRROR_DIR / case_document_guid
            mirror_dir.mkdir(parents=True, exist_ok=True)
            (mirror_dir / f"{blob_id}.png").write_bytes(content_bytes)
        except Exception as e:
            logger.warning(f"[mirror] local save failed: {e}")
        return oss_key
    else:
        save_dir = IMAGE_DIR / case_document_guid
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"{blob_id}.png"
        save_path.write_bytes(content_bytes)
        return f"data/images/{case_document_guid}/{blob_id}.png"


def init_oss_bucket():
    """初始化 OSS bucket，凭证缺失则返回 None。"""
    if not OSS_ACCESS_KEY_ID or not OSS_ACCESS_KEY_SECRET:
        return None
    if oss2 is None:
        logger.warning("[oss] oss2 not installed, falling back to local storage")
        return None
    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    b = oss2.Bucket(auth, f"https://{OSS_ENDPOINT}", OSS_BUCKET_NAME)
    logger.info(f"[oss] ready: bucket={OSS_BUCKET_NAME} endpoint={OSS_ENDPOINT}")
    return b


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="下载 doc_images 中待处理的图片")
    parser.add_argument("--limit", type=int, default=0, help="最多下载几张，0=全部 (默认: 0)")
    args = parser.parse_args()

    ensure_schema()
    rows = get_pending_images(limit=args.limit)
    if not rows:
        logger.info("[done] no pending images")
        return

    logger.info(f"[start] {len(rows)} images to download")

    # 初始化 OSS（凭证缺失则存本地）
    oss_bucket = init_oss_bucket()
    if oss_bucket is None:
        logger.info("[storage] OSS credentials not set, using local storage")

    # 启动 playwright 浏览器并保持
    bm = BrowserManager()
    bm.start()

    session = make_session()
    consecutive_fails = 0

    try:
        for i, (img_id, doc_item_id, case_guid, blob_id, image_url, retry_count) in enumerate(rows):
            logger.info(f"\n[{i+1}/{len(rows)}] img_id={img_id} doc={doc_item_id} case={case_guid} blob={blob_id[:20]}... retries={retry_count}")

            update_image(img_id, STATUS_PROCESSING)

            try:
                status_code, content = fetch_image(session, image_url)
                reason = classify_image_response(status_code, content)
                logger.info(f"  http={status_code} size={len(content):,} reason={reason}")

                # Cloudflare 验证 -> 用 playwright 处理
                if reason == "cloudflare_challenge":
                    logger.info("  [cf] using playwright to solve challenge...")
                    solved = bm.solve_challenge(image_url)
                    session = make_session()
                    if solved:
                        time.sleep(random.uniform(5.0, 10.0))
                        status_code, content = fetch_image(session, image_url)
                        reason = classify_image_response(status_code, content)
                        logger.info(f"  [cf] retry http={status_code} size={len(content):,} reason={reason}")

                    if reason is None:
                        rel_path = save_image_file(case_guid, blob_id, content, oss_bucket=oss_bucket)
                        update_image(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, after cf) -> {rel_path}")
                        consecutive_fails = 0
                    else:
                        update_image(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason})")

                # 登录过期 -> 用 playwright 刷新
                elif reason in ("login_redirect", "http_auth_error"):
                    logger.info(f"  [{reason}] using playwright to refresh login...")
                    bm.refresh_login()
                    session = make_session()
                    time.sleep(random.uniform(3.0, 6.0))
                    status_code, content = fetch_image(session, image_url)
                    reason = classify_image_response(status_code, content)
                    logger.info(f"  [auth] retry http={status_code} size={len(content):,} reason={reason}")

                    if reason is None:
                        rel_path = save_image_file(case_guid, blob_id, content, oss_bucket=oss_bucket)
                        update_image(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                        consecutive_fails = 0
                    else:
                        update_image(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or "auth_failed")
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason or 'auth_failed'})")

                # 正常成功
                elif reason is None:
                    rel_path = save_image_file(case_guid, blob_id, content, oss_bucket=oss_bucket)
                    update_image(img_id, STATUS_SUCCESS, file_path=rel_path, file_size=len(content))
                    logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}) -> {rel_path}")
                    consecutive_fails = 0

                # 不可重试
                elif reason == "not_found":
                    update_image(img_id, STATUS_FAILED, fail_reason=reason)
                    logger.info(f"  -> {STATUS_FAILED} ({STATUS_LABELS[STATUS_FAILED]}, {reason})")
                    consecutive_fails = 0

                # 限速
                elif reason == "rate_limited":
                    update_image(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason}), pausing 60s...")
                    time.sleep(60)

                # 其他失败
                else:
                    update_image(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason})")

            except Exception as e:
                err_reason = f"exception: {type(e).__name__}: {e}"
                logger.info(f"  error: {e}")
                update_image(img_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=err_reason)
                consecutive_fails += 1
                logger.info(f"  -> {STATUS_RETRYABLE} ({err_reason})")

            # 每处理完一张，更新所属文档的 image_status
            update_doc_image_status(doc_item_id)

            # 连续失败熔断
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                logger.info(f"\n[abort] {consecutive_fails} consecutive failures, stopping")
                break

            if i < len(rows) - 1:
                delay = random.uniform(*REQUEST_INTERVAL)
                logger.info(f"  [wait] {delay:.1f}s")
                time.sleep(delay)

    finally:
        # 打印摘要，关闭浏览器
        logger.info("\n[summary]")
        conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT di.status, count(*)
                FROM doc_images di
                JOIN doc_items d ON d.id = di.doc_item_id
                WHERE d.is_primary = TRUE
                GROUP BY di.status ORDER BY di.status
            """)
            for s, c in cur.fetchall():
                logger.info(f"  status={s} ({STATUS_LABELS.get(s, '?')}): {c}")
        conn.close()

        bm.close()


if __name__ == "__main__":
    main()
