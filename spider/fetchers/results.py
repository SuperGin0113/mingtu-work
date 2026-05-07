"""
Westlaw Search/v1/results 分页抓取（纯 XHR）
- 首次登录仍由 sc.py 的 playwright 流程完成 -> 导出 cookies.json
- 业务接口用 curl_cffi（impersonate=chrome120）直接发 XHR：
  * 模拟 Chrome TLS/JA3 指纹、header 顺序
  * 读取 cookies.json 里的 cookie 传入 session
- 两次请求间随机 sleep，模拟人为节奏
- 失效兜底：接口 401/403 或被重定向到登录页时，重新跑一次 playwright 登录并重试
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

from script.project_paths import DATA_DIR
from spider.login.browser_login import COOKIE_TTL, get_authenticated_context
from spider.paths import COOKIE_FILE, STORAGE_FILE



# Referer 来源（从 URL 参数里 resultsPageURL 解出来的页面）
REFERRER_URL = (
    "https://1.next.westlaw.com/Browse/Home/WestKeyNumberSystem"
    "?guid=Icdf1ff7d0510a0608cb0ce83722c4edf&originationContext=documenttoc"
)

PAGE1_FILE = DATA_DIR / "search_results_page1.json"
PAGE2_FILE = DATA_DIR / "search_results_page2.json"

ALLOWED_COOKIE_DOMAINS = (".westlaw.com", "westlaw.com",
                          ".thomsonreuters.com", "thomsonreuters.com",
                          ".next.westlaw.com", "next.westlaw.com",
                          "1.next.westlaw.com", "signon.thomsonreuters.com")


def _cookies_valid() -> bool:
    if not COOKIE_FILE.exists():
        return False
    age = time.time() - COOKIE_FILE.stat().st_mtime
    if age > COOKIE_TTL:
        print(f"[cookies] cookies.json 已过期 (age={int(age)}s)")
        return False
    return True


_BROWSER_STATE = {"pw": None, "browser": None, "context": None}


def _run_playwright_login():
    """启动 playwright 走 sc.py 的登录流程生成 cookies.json，然后保留浏览器不关闭。"""
    print("[login] 调用 sc.py 的 playwright 登录流程以刷新 cookies...")
    pw = sync_playwright().start()
    browser, context, _page = get_authenticated_context(pw)
    # 显式再导出一次 cookies，确保 cookies.json 是最新的
    cookies = context.cookies()
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[login] cookies 已刷新 -> {COOKIE_FILE}  (共 {len(cookies)} 条)")

    _BROWSER_STATE["pw"] = pw
    _BROWSER_STATE["browser"] = browser
    _BROWSER_STATE["context"] = context
    print("[login] 浏览器保持打开，脚本结束后可继续操作。")


def _shutdown_browser():
    """仅在用户显式要求时调用：关闭浏览器 & playwright。"""
    for key in ("context", "browser"):
        obj = _BROWSER_STATE.get(key)
        if obj:
            try:
                obj.close()
            except Exception:
                pass
    pw = _BROWSER_STATE.get("pw")
    if pw:
        try:
            pw.stop()
        except Exception:
            pass
    _BROWSER_STATE.update({"pw": None, "browser": None, "context": None})


def ensure_cookies() -> list:
    """确保 cookies.json 可用（必要时触发 playwright 登录），返回 cookie 列表（playwright 原始格式）。"""
    if not _cookies_valid():
        _run_playwright_login()
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    print(f"[cookies] 加载 {len(cookies)} 条 cookies from {COOKIE_FILE}")
    return cookies


def build_session(cookies: list) -> cffi_requests.Session:
    """构造 curl_cffi Session：模拟 Chrome120 指纹 + 注入 cookies + 默认 headers。"""
    session = cffi_requests.Session(impersonate="chrome120")
    injected = 0
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        if not any(domain == d or domain.endswith(d) for d in ALLOWED_COOKIE_DOMAINS):
            continue
        try:
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
            injected += 1
        except Exception as e:
            print(f"[cookies] 注入 {c.get('name')} 失败: {e}")
    print(f"[session] 注入 cookies {injected}/{len(cookies)}  impersonate=chrome120")

    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": REFERRER_URL,
        "Origin": "https://1.next.westlaw.com",
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


def fetch_page(session: cffi_requests.Session, url: str) -> dict:
    """发一次 GET，统一返回 {status, content_type, body}。"""
    resp = session.get(url, timeout=30)
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": resp.text,
    }


def is_session_expired(result: dict) -> bool:
    """
    判断响应是否表示登录态失效。
    触发：HTTP 401/403/419/440；返回 HTML 而非 JSON；body 含登录/签退关键字。
    """
    status = result.get("status", 0)
    if status in (401, 403, 419, 440):
        return True
    ctype = (result.get("content_type") or "").lower()
    body = result.get("body") or ""
    if "json" not in ctype and "html" in ctype:
        return True
    lowered = body[:4000].lower()
    for marker in ("signon.thomsonreuters", "sessionexpired", "please sign in",
                   "your session has expired", "loginform"):
        if marker in lowered:
            return True
    return False


def force_refresh_session() -> cffi_requests.Session:
    """清掉过期缓存，重跑 playwright 登录拿新 cookie，再重建 session。"""
    print("[auth] 会话失效，清除缓存并重新登录...")
    for f in (STORAGE_FILE, COOKIE_FILE):
        try:
            if f.exists():
                f.unlink()
                print(f"[auth] 已删除: {f}")
        except Exception as e:
            print(f"[auth] 删除 {f} 失败: {e}")
    cookies = ensure_cookies()
    return build_session(cookies)


def fetch_with_retry(session: cffi_requests.Session, url: str, tag: str):
    """fetch 一次；若判定会话过期则刷新 session 后重试 1 次。"""
    result = fetch_page(session, url)
    if is_session_expired(result):
        print(f"[auth] {tag} 响应疑似失效 status={result.get('status')} "
              f"ct={result.get('content_type')}")
        session = force_refresh_session()
        time.sleep(random.uniform(2.0, 4.0))
        result = fetch_page(session, url)
    return session, result


def save_response(path: Path, url: str, result: dict):
    body = result.get("body", "")
    parsed = None
    if "json" in (result.get("content_type") or "").lower():
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None

    record = {
        "url": url,
        "status": result.get("status"),
        "content_type": result.get("content_type"),
        "body_raw": body if parsed is None else None,
        "body_json": parsed,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[save] {path}  status={result.get('status')}  bytes={len(body)}")


def main():
    cookies = ensure_cookies()
    session = build_session(cookies)

    # 第 1 页
    delay1 = random.uniform(2.0, 4.0)
    print(f"[wait] 请求前随机等待 {delay1:.1f}s")
    time.sleep(delay1)
    print(f"[fetch] page 1 -> {URL_PAGE_1[:120]}...")
    session, r1 = fetch_with_retry(session, URL_PAGE_1, "page1")
    save_response(PAGE1_FILE, URL_PAGE_1, r1)

    # 分页间隔
    delay2 = random.uniform(5.0, 9.0)
    print(f"[wait] 分页间隔 {delay2:.1f}s")
    time.sleep(delay2)

    # 第 2 页
    print(f"[fetch] page 2 -> {URL_PAGE_2[:120]}...")
    session, r2 = fetch_with_retry(session, URL_PAGE_2, "page2")
    save_response(PAGE2_FILE, URL_PAGE_2, r2)

    # 摘要
    print("\n========= 摘要 =========")
    for tag, r, f in (("page1", r1, PAGE1_FILE), ("page2", r2, PAGE2_FILE)):
        print(f"{tag}: status={r['status']}  ct={r['content_type']}  file={f}")
        body = r.get("body", "")
        print(f"  body[:300]: {body[:300]}")

    # 本次跑过登录流程（浏览器还开着）则进入交互等待；否则直接退出
    if _BROWSER_STATE.get("browser"):
        print("\n" + "=" * 60)
        print("浏览器保持打开。输入 q / quit / exit 关闭浏览器并退出脚本；")
        print("回车则继续保持打开。")
        print("=" * 60)
        while True:
            try:
                ans = input("是否关闭浏览器并退出? [q/回车]: ").strip().lower()
            except EOFError:
                break
            if ans in ("q", "quit", "exit", "y", "yes"):
                _shutdown_browser()
                break
            print(f"[keep] 浏览器保持打开。当前 cookies 文件: {COOKIE_FILE}")


if __name__ == "__main__":
    main()
