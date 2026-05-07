"""
Westlaw 爬虫 - 登录与 Cookie 缓存管理
目标站点: https://1.next.westlaw.com/Search/Home.html

依赖:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from script.project_paths import DATA_DIR, ENV_FILE, ROOT_DIR
from spider.paths import COOKIE_FILE, STORAGE_FILE

BASE_DIR = ROOT_DIR
load_dotenv(ENV_FILE)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(f"Missing required env var: {name}")


# ========== 配置 ==========
USERNAME = _require_env("WESTLAW_USERNAME")
PASSWORD = _require_env("WESTLAW_PASSWORD")

BASE_URL = "https://1.next.westlaw.com"
HOME_URL = f"{BASE_URL}/Search/Home.html?transitionType=Default&contextData=(sc.Default)"
SIGNON_URL = "https://signon.thomsonreuters.com/?productid=WLN&returnto=https%3A%2F%2F1.next.westlaw.com%2FCosi%2FSignOn"

DATA_DIR.mkdir(exist_ok=True)
RESPONSES_FILE = DATA_DIR / "responses.jsonl"
CLIENT_ID_PAGE_HTML = DATA_DIR / "client_id_page.html"
HOME_PAGE_HTML = DATA_DIR / "home_page.html"
RECENT_RESEARCH_FILE = DATA_DIR / "recent_research.json"

# Cookie 有效期（秒），到期后强制重新登录
COOKIE_TTL = 6 * 3600  # 6 小时

HEADLESS = False  # 调试时设为 False，稳定后可改为 True
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ========== Cookie 缓存工具 ==========
def load_storage_state():
    """加载缓存的登录状态 (cookies + localStorage)"""
    if not STORAGE_FILE.exists():
        return None
    # 检查 TTL
    age = time.time() - STORAGE_FILE.stat().st_mtime
    if age > COOKIE_TTL:
        print(f"[cache] Cookie 已过期 (age={int(age)}s)，需要重新登录")
        return None
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        print(f"[cache] 加载缓存登录状态: {STORAGE_FILE}")
        return state
    except Exception as e:
        print(f"[cache] 读取缓存失败: {e}")
        return None


def save_storage_state(context):
    """保存当前上下文的登录状态"""
    context.storage_state(path=str(STORAGE_FILE))
    # 同时单独导出 cookies 方便其他工具使用
    cookies = context.cookies()
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[cache] 已保存登录状态 -> {STORAGE_FILE}")
    print(f"[cache] 已保存 cookies     -> {COOKIE_FILE}")


# ========== 登录状态检测 ==========
def is_logged_in(page) -> bool:
    """
    判断当前是否已登录。
    策略：访问首页后若 URL 仍在 westlaw 域名且未被重定向到 signon，则认为已登录。
    同时检查页面是否出现登录表单字段。
    """
    current_url = page.url.lower()
    if "signon" in current_url or "signin" in current_url or "login" in current_url:
        return False
    if "westlaw.com" not in current_url:
        return False
    # 检查页面是否存在用户名输入框（未登录才会出现）
    try:
        has_username_input = page.locator("#Username, input[name='Username']").count() > 0
        if has_username_input:
            return False
    except Exception:
        pass
    return True


# ========== 登录流程 ==========
def do_login(page):
    """执行 Westlaw 登录流程"""
    print("[login] 打开登录页...")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)

    # 若未登录会被重定向到 signon.thomsonreuters.com
    page.wait_for_load_state("networkidle", timeout=60_000)

    # 1) 输入用户名
    print("[login] 填写用户名...")
    username_input = page.locator("#Username, input[name='Username']").first
    username_input.wait_for(state="visible", timeout=30_000)
    username_input.fill(USERNAME)

    # 部分流程是"先用户名 -> 下一步 -> 再密码"，也可能同页两字段
    password_input = page.locator("#Password, input[name='Password']").first
    if password_input.count() == 0 or not password_input.is_visible():
        # 点击"下一步" / "继续"
        next_btn = page.locator(
            "button:has-text('Sign In'), button:has-text('Next'), "
            "button:has-text('Continue'), #SignIn, input[type='submit']"
        ).first
        if next_btn.count() > 0:
            next_btn.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
        password_input = page.locator("#Password, input[name='Password']").first
        password_input.wait_for(state="visible", timeout=30_000)

    # 2) 输入密码
    print("[login] 填写密码...")
    password_input.fill(PASSWORD)

    # 3) 提交
    submit_btn = page.locator(
        "#SignIn, button:has-text('Sign In'), button:has-text('Sign in'), "
        "input[type='submit']"
    ).first
    submit_btn.click()
    print("[login] 已提交登录表单，等待跳转...")

    # 4) 等待回到 westlaw 主域
    try:
        page.wait_for_url("**/westlaw.com/**", timeout=60_000)
    except PlaywrightTimeoutError:
        print("[login] 等待跳转超时，继续尝试...")

    page.wait_for_load_state("networkidle", timeout=60_000)

    # Client ID 确认页（Welcome, XXX + Client ID 下拉 + Continue）
    handle_client_id_page(page)

    if not is_logged_in(page):
        raise RuntimeError("登录失败：未能跳转到已登录状态的 Westlaw 页面")
    print("[login] 登录成功")


# ========== Client ID 确认页 ==========
def handle_client_id_page(page):
    """
    处理登录后出现的 "Welcome, XXX / Client ID / Continue" 页面。
    1. 保存该页的 HTML 和 recent research 列表
    2. 点击 Continue 进入真正的工作台
    """
    try:
        # 页面特征：存在名为 ClientID 的 select 或 id 包含 Client
        client_id_el = page.locator(
            "select[name='ClientID'], select#ClientID, "
            "input[name='ClientID'], input#ClientID"
        ).first
        has_client_id = client_id_el.count() > 0

        continue_btn = page.locator(
            "button:has-text('Continue'), input[type='submit'][value*='Continue' i], "
            "#co_clientIDContinueButton, button#continueButton"
        ).first

        if not has_client_id and continue_btn.count() == 0:
            return  # 不是 Client ID 页，跳过

        print("[client-id] 检测到 Client ID 确认页，保存页面信息...")

        # 保存 HTML 快照
        try:
            CLIENT_ID_PAGE_HTML.write_text(page.content(), encoding="utf-8")
            print(f"[client-id] 已保存 HTML -> {CLIENT_ID_PAGE_HTML}")
        except Exception as e:
            print(f"[client-id] 保存 HTML 失败: {e}")

        # 抓取 "Return to your recent research" 列表
        recent_items = extract_recent_research(page)
        if recent_items:
            RECENT_RESEARCH_FILE.write_text(
                json.dumps(recent_items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[client-id] 已保存 {len(recent_items)} 条 recent research -> {RECENT_RESEARCH_FILE}")

        # 点击 Continue
        if continue_btn.count() > 0 and continue_btn.is_visible():
            print("[client-id] 点击 Continue...")
            with page.expect_navigation(wait_until="networkidle", timeout=60_000):
                continue_btn.click()
        else:
            # 兜底：提交所在 form
            page.evaluate(
                "() => { const f = document.querySelector('form'); if (f) f.submit(); }"
            )
            page.wait_for_load_state("networkidle", timeout=60_000)

        print(f"[client-id] Continue 后 URL: {page.url}")
    except Exception as e:
        print(f"[client-id] 处理异常: {e}")


def extract_recent_research(page):
    """从 Client ID 页右侧抓取最近的检索/案件列表"""
    try:
        return page.evaluate(
            """
            () => {
                const items = [];
                // 尝试多种选择器匹配右侧列表项
                const nodes = document.querySelectorAll(
                    "a[href*='Document'], a[href*='Search'], li.co_item, div.co_recentItem, tr"
                );
                const seen = new Set();
                nodes.forEach(n => {
                    const text = (n.innerText || "").trim();
                    if (!text || text.length < 5 || seen.has(text)) return;
                    seen.add(text);
                    const link = n.tagName === 'A' ? n : n.querySelector('a');
                    items.push({
                        text: text.slice(0, 500),
                        href: link ? link.href : null,
                    });
                });
                return items;
            }
            """
        )
    except Exception as e:
        print(f"[recent] 抓取 recent research 失败: {e}")
        return []


# ========== 网络响应抓取 ==========
def attach_response_logger(context):
    """
    监听所有响应，把 XHR/JSON 写入 responses.jsonl。
    每行一个 JSON：{url, status, headers, body}
    """
    fp = open(RESPONSES_FILE, "a", encoding="utf-8")

    def _on_response(response):
        try:
            url = response.url
            ctype = (response.headers or {}).get("content-type", "")
            # 只记录 API / JSON / HTML 这类有意义的响应，过滤静态资源
            if not any(k in ctype.lower() for k in ("json", "javascript", "html", "xml")):
                return
            if any(skip in url for skip in (".js?", ".css", "/static/", "/images/")):
                return
            record = {
                "url": url,
                "status": response.status,
                "content_type": ctype,
                "method": response.request.method,
            }
            # 仅对较小的 JSON 响应保存 body
            if "json" in ctype.lower() and response.status == 200:
                try:
                    text = response.text()
                    if len(text) < 200_000:
                        record["body"] = text
                except Exception:
                    pass
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            fp.flush()
        except Exception:
            pass

    context.on("response", _on_response)
    return fp


# ========== 主入口 ==========
def get_authenticated_context(playwright):
    """返回一个已登录的 browser context"""
    browser = playwright.chromium.launch(headless=HEADLESS)
    storage = load_storage_state()

    context = browser.new_context(
        storage_state=storage if storage else None,
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
    )
    # 挂载响应日志
    attach_response_logger(context)

    page = context.new_page()

    # 先尝试用缓存访问
    print(f"[nav] 访问首页: {HOME_URL}")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        pass

    if is_logged_in(page):
        print("[status] 已使用缓存 Cookie 登录")
    else:
        print("[status] 未登录，执行登录流程...")
        do_login(page)
        save_storage_state(context)

    # 保存登录后首页 HTML
    try:
        HOME_PAGE_HTML.write_text(page.content(), encoding="utf-8")
        print(f"[home] 已保存首页 HTML -> {HOME_PAGE_HTML}")
    except Exception as e:
        print(f"[home] 保存首页 HTML 失败: {e}")

    return browser, context, page


def main():
    with sync_playwright() as p:
        browser, context, page = get_authenticated_context(p)
        try:
            print(f"[done] 当前 URL: {page.url}")
            print(f"[done] 页面标题: {page.title()}")
            cookies = context.cookies()
            print(f"[done] 共 {len(cookies)} 个 cookies")

            # 登录完成后，保持浏览器打开，等待用户决定何时退出
            print("\n" + "=" * 60)
            print("登录完成。浏览器保持打开，你可以继续手动操作或调试。")
            print("输入 q / quit / exit 退出；回车刷新并再次询问。")
            print("=" * 60)
            while True:
                try:
                    ans = input("是否退出? [y/N]: ").strip().lower()
                except EOFError:
                    break
                if ans in ("y", "yes", "q", "quit", "exit"):
                    print("[exit] 保存最新登录状态并退出...")
                    try:
                        save_storage_state(context)
                    except Exception as e:
                        print(f"[exit] 保存状态失败: {e}")
                    break
                else:
                    print(f"[keep] 继续保持。当前 URL: {page.url}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
