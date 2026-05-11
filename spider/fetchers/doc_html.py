"""
从 doc_items 表取 is_primary 待处理记录，用缓存的 cookie 下载 docUrl 对应的 HTML，写入 doc_html 字段。
状态机：0 待处理 -> 1 处理中 -> 2 成功 / 3 可重试 / 4 不可重试
# 原有旧逻辑，暂保留
架构：
- 启动时打开 playwright 浏览器并保持，用于登录刷新 cookie 和处理人机验证
- 数据请求用 curl_cffi（更快、更轻量）
- 遇到 CF 验证或登录过期时，用 playwright 浏览器处理后提取新 cookie
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import random
import re
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import psycopg2
from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

from script.db import DB_CONFIG, DB_NAME
from script.project_paths import DATA_DIR
from spider.login.browser_login import COOKIE_TTL, get_authenticated_context
from spider.paths import COOKIE_FILE, STORAGE_FILE

# 日志配置：终端 + 文件同时输出
LOG_DIR = DATA_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "fetch_doc.log"

logger = logging.getLogger("fetch_doc")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

TABLE = "doc_items"

BASE_URL = "https://1.next.westlaw.com"
REQUEST_INTERVAL = (20.0, 30.0)

# status 状态码
STATUS_PENDING = 0       # 待处理
STATUS_PROCESSING = 1    # 处理中
STATUS_SUCCESS = 2       # 处理成功
STATUS_RETRYABLE = 3     # 处理失败（可重试）
STATUS_FAILED = 4        # 处理失败（不可重试）

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
        返回页面 HTML 内容（验证通过后的真实页面）。
        """
        logger.info(f"[browser] navigating to solve challenge: {url[:100]}...")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待 CF 验证通过：检测页面不再包含 challenge 标记
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
                    return content

                if attempt % 5 == 4:
                    logger.info(f"[browser] still waiting for challenge... ({(attempt+1)*3}s)")

            logger.info("[browser] challenge timeout after 90s")
            self._save_cookies()
            return None

        except Exception as e:
            logger.info(f"[browser] solve_challenge error: {e}")
            self._save_cookies()
            return None

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
                # 需要重新走完整登录流程
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/Search/Home.html",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    return session


# ==================== 响应分类 ====================

def classify_response(result):
    """判断响应类型，返回失败原因字符串，正常则返回 None。"""
    status_code = result.get("status", 0)
    body_lower = (result.get("body") or "")[:5000].lower()

    for marker in ("cf-browser-verification", "challenge-platform", "cf-chl-bypass",
                   "just a moment", "checking your browser"):
        if marker in body_lower:
            return "cloudflare_challenge"

    for marker in ("signon.thomsonreuters", "cosi/signon", "sessionexpired",
                   "please sign in", "your session has expired", "loginform",
                   "productid=cbt", "redirectto"):
        if marker in body_lower:
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


# ==================== DB ====================

def ensure_table():
    """确保表结构完整，恢复中断状态。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0")
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS fail_reason TEXT")
        cur.execute(f"""
            ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS create_time TIMESTAMPTZ
            NOT NULL DEFAULT CURRENT_TIMESTAMP
        """)
        cur.execute(f"""
            ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS update_time TIMESTAMPTZ
            NOT NULL DEFAULT CURRENT_TIMESTAMP
        """)
        cur.execute(f"""
            UPDATE {TABLE}
            SET create_time = COALESCE(create_time, CURRENT_TIMESTAMP),
                update_time = COALESCE(update_time, CURRENT_TIMESTAMP)
            WHERE create_time IS NULL OR update_time IS NULL
        """)

        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE is_primary = TRUE")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE is_primary = TRUE AND status = %s", (STATUS_SUCCESS,))
        done = cur.fetchone()[0]
        logger.info(f"[init] {TABLE}: {done}/{total} primary done")

        # 把上次中断卡在 processing 的 primary 记录重置为 pending
        cur.execute(
            f"UPDATE {TABLE} SET status = %s, update_time = CURRENT_TIMESTAMP "
            f"WHERE is_primary = TRUE AND status = %s",
            (STATUS_PENDING, STATUS_PROCESSING),
        )
        if cur.rowcount:
            logger.info(f"[init] reset {cur.rowcount} stale processing rows to pending")
    conn.close()


def get_pending_rows():
    """取 is_primary 的待处理(0) + 可重试(3) 记录，排除已超过重试上限的。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, rank, doc_url, case_document_guid, retry_count FROM {TABLE} "
            f"WHERE is_primary = TRUE AND status IN (%s, %s) AND retry_count < %s ORDER BY rank",
            (STATUS_PENDING, STATUS_RETRYABLE, MAX_RETRIES),
        )
        rows = cur.fetchall()
        # 超过重试上限的 primary 记录直接转 FAILED
        cur.execute(
            f"UPDATE {TABLE} SET status = %s "
            f"WHERE is_primary = TRUE AND status = %s AND retry_count >= %s",
            (STATUS_FAILED, STATUS_RETRYABLE, MAX_RETRIES),
        )
        if cur.rowcount:
            logger.info(f"[init] {cur.rowcount} rows exceeded max retries({MAX_RETRIES}), marked as failed")
    conn.commit()
    conn.close()
    return rows


def update_row(row_id, status, doc_html=None, inc_retry=False, fail_reason=None):
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        if doc_html is not None:
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET status = %s, doc_html = %s,
                    fail_reason = NULL, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (status, doc_html, row_id),
            )
        elif inc_retry:
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET status = %s, retry_count = retry_count + 1,
                    fail_reason = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (status, fail_reason, row_id),
            )
        else:
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET status = %s, fail_reason = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (status, fail_reason, row_id),
            )
    conn.close()


def all_primary(rows):
    """校验查询结果全部是 is_primary 记录。"""
    if not rows:
        return True
    ids = [r[0] for r in rows]
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {TABLE} WHERE id = ANY(%s) AND is_primary = FALSE",
            (ids,),
        )
        bad = cur.fetchone()[0]
    conn.close()
    if bad:
        logger.error(f"[check] found {bad} non-primary rows in pending list!")
        return False
    return True


# ==================== 请求 ====================

def _normalize_html(raw_html):
    """压缩连续空行，保持与浏览器复制一致。"""
    raw_html = raw_html.replace('\r\n', '\n').replace('\r', '\n')
    raw_html = re.sub(r'\n{3,}', '\n\n', raw_html)
    return raw_html.strip()


# ==================== 图片解析 ====================

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


def save_content_images(row_id, doc_html):
    """解析 HTML 中的 content images，写入 doc_images 表并更新主表 image_count。"""
    images = extract_content_images(doc_html)
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        for img in images:
            cur.execute(
                """
                INSERT INTO doc_images (doc_item_id, blob_id, image_url, alt_text, width, height, position)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_item_id, blob_id) DO NOTHING
                """,
                (row_id, img["blob_id"], img["image_url"], img["alt_text"],
                 img["width"], img["height"], img["position"]),
            )
        cur.execute(
            "UPDATE doc_items SET image_count = %s WHERE id = %s",
            (len(images), row_id),
        )
    conn.close()
    if images:
        logger.info(f"  [images] extracted {len(images)} content images")


def fetch_doc(session, url):
    resp = session.get(url, timeout=60, allow_redirects=True)
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": _normalize_html(resp.text),
    }


# ==================== 主流程 ====================

def main():
    ensure_table()
    rows = get_pending_rows()
    if not rows:
        logger.info("[done] no pending rows")
        return

    # 强校验：只处理 is_primary 的记录
    assert all_primary(rows), "FATAL: get_pending_rows returned non-primary rows!"
    logger.info(f"[check] all {len(rows)} rows verified as is_primary")

    # 启动 playwright 浏览器并保持
    bm = BrowserManager()
    bm.start()

    session = make_session()
    consecutive_fails = 0

    try:
        for i, (row_id, rank, doc_url, guid, retry_count) in enumerate(rows):
            logger.info(f"\n[{i+1}/{len(rows)}] rank={rank} id={row_id} guid={guid} retries={retry_count}")
            logger.info(f"  url={doc_url[:120]}...")

            update_row(row_id, STATUS_PROCESSING)

            try:
                result = fetch_doc(session, doc_url)
                status_code = result["status"]
                body_len = len(result["body"])
                reason = classify_response(result)
                logger.info(f"  http={status_code} len={body_len} reason={reason}")

                # Cloudflare 验证 -> 用 playwright 处理
                if reason == "cloudflare_challenge":
                    logger.info("  [cf] using playwright to solve challenge...")
                    html = bm.solve_challenge(doc_url)
                    session = make_session()  # 用新 cookie 重建 session
                    if html and len(html) > 1000:
                        # playwright 直接拿到了页面内容
                        update_row(row_id, STATUS_SUCCESS, doc_html=html)
                        save_content_images(row_id, html)
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, solved by playwright)")
                        consecutive_fails = 0
                    else:
                        # playwright 也没过验证，用新 cookie 重试一次
                        time.sleep(random.uniform(5.0, 10.0))
                        result = fetch_doc(session, doc_url)
                        reason2 = classify_response(result)
                        if reason2 is None and result["status"] == 200 and len(result["body"]) > 500:
                            update_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                            save_content_images(row_id, result["body"])
                            logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]}, retry after cf)")
                            consecutive_fails = 0
                        else:
                            update_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason="cloudflare_unsolved")
                            consecutive_fails += 1
                            logger.info(f"  -> {STATUS_RETRYABLE} (cloudflare_unsolved)")

                # 登录过期 -> 用 playwright 刷新
                elif reason in ("login_redirect", "http_auth_error"):
                    logger.info(f"  [{reason}] using playwright to refresh login...")
                    bm.refresh_login()
                    session = make_session()
                    time.sleep(random.uniform(3.0, 6.0))
                    result = fetch_doc(session, doc_url)
                    status_code = result["status"]
                    body_len = len(result["body"])
                    reason = classify_response(result)
                    logger.info(f"  [auth] retry http={status_code} len={body_len} reason={reason}")

                    if reason is None and status_code == 200 and body_len > 500:
                        update_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                        save_content_images(row_id, result["body"])
                        logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                        consecutive_fails = 0
                    else:
                        update_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or "auth_failed")
                        consecutive_fails += 1
                        logger.info(f"  -> {STATUS_RETRYABLE} ({reason or 'auth_failed'})")

                # 正常成功
                elif reason is None and status_code == 200 and body_len > 500:
                    update_row(row_id, STATUS_SUCCESS, doc_html=result["body"])
                    save_content_images(row_id, result["body"])
                    logger.info(f"  -> {STATUS_SUCCESS} ({STATUS_LABELS[STATUS_SUCCESS]})")
                    consecutive_fails = 0

                # 不可重试
                elif reason == "not_found":
                    update_row(row_id, STATUS_FAILED, fail_reason=reason)
                    logger.info(f"  -> {STATUS_FAILED} ({STATUS_LABELS[STATUS_FAILED]}, {reason})")
                    consecutive_fails = 0

                # 限速
                elif reason == "rate_limited":
                    update_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason)
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason}), pausing 60s...")
                    time.sleep(60)

                # 其他失败
                else:
                    update_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=reason or f"http_{status_code}")
                    consecutive_fails += 1
                    logger.info(f"  -> {STATUS_RETRYABLE} ({reason or f'http_{status_code}'})")

            except Exception as e:
                err_reason = f"exception: {type(e).__name__}: {e}"
                logger.info(f"  error: {e}")
                update_row(row_id, STATUS_RETRYABLE, inc_retry=True, fail_reason=err_reason)
                consecutive_fails += 1
                logger.info(f"  -> {STATUS_RETRYABLE} ({err_reason})")

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
            cur.execute(f"""
                SELECT status, count(*) FROM {TABLE}
                WHERE is_primary = TRUE
                GROUP BY status ORDER BY status
            """)
            for s, c in cur.fetchall():
                logger.info(f"  status={s} ({STATUS_LABELS.get(s, '?')}): {c}")
        conn.close()

        bm.close()


if __name__ == "__main__":
    main()
