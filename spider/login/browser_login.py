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
from playwright_stealth import Stealth

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
def _settle_dom(page, max_wait: float = 8.0) -> None:
    """轻量等待页面稳定：等 DOM 加载完成，再做一次短的 networkidle 尝试（容错）。

    Westlaw 上常驻分析/长连接，networkidle 经常永不达成；不要把它当硬阻塞。
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=int(max_wait * 1000))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=2_000)
    except Exception:
        pass


def _wait_for_url_settled(page, timeout: float = 30.0, stable_seconds: float = 1.5) -> str:
    """轮询 page.url，直到 URL 在 stable_seconds 内不变 或 超时。返回最终 URL。"""
    deadline = time.time() + timeout
    last_url = page.url
    last_change = time.time()
    while time.time() < deadline:
        cur = page.url
        if cur != last_url:
            last_url = cur
            last_change = time.time()
        elif time.time() - last_change >= stable_seconds:
            break
        time.sleep(0.25)
    return page.url


def _has_signon_form(page) -> bool:
    try:
        return page.locator("#Username, input[name='Username']").count() > 0
    except Exception:
        return False


def _has_client_id_page(page) -> bool:
    """Welcome, XXX / Client ID 输入框 / Continue 这种过渡页（lightbox 弹窗）。

    必须 element 存在 **且可见**——Continue 被点掉后 lightbox 关闭，元素仍在 DOM
    里只是隐藏，那时不能再判定为 client_id 页。
    """
    candidates = (
        "#co_clientIDContinueButton",
        "#co_clientIDTextbox",
        "input[name='clientIdTextbox']",
        "select[name='ClientID']",
        "select#ClientID",
        "input[name='ClientID']",
        "input#ClientID",
    )
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _classify_page(page) -> str:
    """返回 'signon' | 'client_id' | 'logged_in' | 'unknown'"""
    url = (page.url or "").lower()
    if "signon" in url or "signin" in url or "/login" in url:
        return "signon"
    if _has_signon_form(page):
        return "signon"
    if "westlaw.com" not in url:
        return "unknown"
    if _has_client_id_page(page):
        return "client_id"
    return "logged_in"


def is_logged_in(page) -> bool:
    """已登录 = 处于 westlaw 域且非 signon 页、非 Client ID 过渡页。"""
    return _classify_page(page) == "logged_in"


# ========== 登录流程 ==========
def do_login(page):
    """执行 Westlaw 登录流程"""
    print("[login] 打开登录页...")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    _settle_dom(page)

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
            _settle_dom(page)
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

    # 4) 轮询等待跳转出 signon 域（不依赖 networkidle）
    deadline = time.time() + 90.0
    while time.time() < deadline:
        url_l = (page.url or "").lower()
        if "westlaw.com" in url_l and "signon" not in url_l and "signin" not in url_l:
            break
        time.sleep(0.5)
    _settle_dom(page)
    final_url = _wait_for_url_settled(page, timeout=15.0)
    print(f"[login] 跳转后 URL: {final_url[:160]}")

    # Client ID 确认页（Welcome, XXX + Client ID 下拉 + Continue）
    if _has_client_id_page(page):
        handle_client_id_page(page)
        _settle_dom(page)

    if not is_logged_in(page):
        raise RuntimeError(f"登录失败：未能跳转到已登录状态的 Westlaw 页面 (url={page.url[:160]})")
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

        # 0) 先把可能挡住按钮的 OneTrust cookie 弹窗点掉（如有）
        _dismiss_consent_banners(page)

        # 1) 确保 Client ID 输入框有值（页面通常预填，没填则 fallback 用 USERNAME 当作 client）
        try:
            cid_input = page.locator("#co_clientIDTextbox, input[name='clientIdTextbox']").first
            if cid_input.count() > 0:
                cur_val = cid_input.input_value() or ""
                if not cur_val.strip():
                    fallback = os.getenv("WESTLAW_CLIENT_ID") or USERNAME
                    print(f"[client-id] 输入框为空，填入 fallback: {fallback!r}")
                    cid_input.fill(fallback)
        except Exception as e:
            print(f"[client-id] 处理输入框异常: {e}")

        # 2) 点击 Continue。Continue 是 JS 绑定的 input[type=button]，不一定触发 navigation。
        before_url = page.url
        clicked = False
        if continue_btn.count() > 0:
            try:
                continue_btn.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            try:
                print("[client-id] 点击 Continue (Playwright click)...")
                continue_btn.click(timeout=10_000, force=True)
                clicked = True
            except Exception as e:
                print(f"[client-id] click 失败: {e}; 尝试 JS 触发")

        if not clicked:
            try:
                ok = page.evaluate(
                    "() => { const b = document.getElementById('co_clientIDContinueButton'); "
                    "if (!b) return false; b.click(); return true; }"
                )
                print(f"[client-id] JS 触发 Continue: {ok}")
            except Exception as e:
                print(f"[client-id] JS 触发失败: {e}")

        # 3) 等待页面真正离开 Client ID 状态：
        #    URL 改变 OR Continue 按钮消失 OR firstPage=true 从 URL 中消失
        deadline = time.time() + 30.0
        while time.time() < deadline:
            now_url = page.url
            url_changed = now_url != before_url
            no_first_page = "firstpage=true" not in now_url.lower()
            try:
                btn_visible = continue_btn.is_visible()
            except Exception:
                btn_visible = False
            if (url_changed and no_first_page) or not btn_visible:
                break
            time.sleep(0.5)
        else:
            # 30s 还没动 → 再用 JS 触发一次试试
            try:
                page.evaluate(
                    "() => { const b = document.getElementById('co_clientIDContinueButton'); "
                    "if (b) b.click(); }"
                )
            except Exception:
                pass
            time.sleep(3)

        _wait_for_url_settled(page, timeout=15.0)
        _settle_dom(page)
        print(f"[client-id] Continue 后 URL: {page.url[:160]}")
    except Exception as e:
        print(f"[client-id] 处理异常: {e}")


def _dismiss_consent_banners(page) -> None:
    """OneTrust / 其他 cookie 弹窗会盖住 Continue 按钮，先点掉。"""
    selectors = (
        "#onetrust-accept-btn-handler",
        "button#accept-recommended-btn-handler",
        "button.onetrust-close-btn-handler",
        "#onetrust-pc-btn-handler",  # 可能是 Settings; 一般不点
    )
    for sel in selectors[:3]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2_000)
                print(f"[client-id] 已点掉 cookie banner: {sel}")
                time.sleep(0.5)
                return
        except Exception:
            continue


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
    # 反检测:对 context 应用 playwright-stealth 的 evasion 脚本
    # (navigator.webdriver / chrome.runtime / canvas / WebGL 等),帮助过 Cloudflare BM。
    Stealth().apply_stealth_sync(context)

    # 挂载响应日志
    attach_response_logger(context)

    page = context.new_page()

    used_cache = False
    if storage:
        # 优先尝试缓存：访问首页，看落地是 signon / Client ID / 已登录
        print(f"[nav] 访问首页 (使用缓存): {HOME_URL}")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            print("[nav] 首页 domcontentloaded 超时，继续按当前 URL 判断")
        _settle_dom(page)
        _wait_for_url_settled(page, timeout=15.0)

        kind = _classify_page(page)
        print(f"[status] 缓存落地页类型: {kind} (url={page.url[:160]})")

        if kind == "client_id":
            # 这只是登录后的过渡页，点 Continue 即可，不用重登
            handle_client_id_page(page)
            _settle_dom(page)
            kind = _classify_page(page)
            print(f"[status] Continue 后类型: {kind}")

        if kind == "logged_in":
            print("[status] ✓ 缓存有效，跳过登录")
            used_cache = True
        else:
            print(f"[status] ✗ 缓存不可用 (kind={kind})，执行登录流程...")

    if not used_cache:
        if not storage:
            print("[status] 无缓存，执行登录流程...")
        do_login(page)
        save_storage_state(context)

    # 保存登录后首页 HTML
    try:
        HOME_PAGE_HTML.write_text(page.content(), encoding="utf-8")
        print(f"[home] 已保存首页 HTML -> {HOME_PAGE_HTML}")
    except Exception as e:
        print(f"[home] 保存首页 HTML 失败: {e}")

    return browser, context, page


def browser_login_once() -> int:
    """启动 chromium → 跑完登录（或仅校验缓存） → 保存 cookies → 关闭浏览器。

    供 ensure_logged_in() 在 HTTP 登录失败时 fallback 使用。
    返回写入的 cookie 数量。
    """
    with sync_playwright() as p:
        browser, context, _page = get_authenticated_context(p)
        try:
            save_storage_state(context)
            cookies = context.cookies()
            return len(cookies)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


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
