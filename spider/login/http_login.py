"""纯 HTTP 走 thomsonreuters signon → westlaw 登录流程。

成功 → tmp/cookies.json 写入 playwright 兼容的 cookie list 格式；同时写最简 storage_state.json
失败 → raise NeedBrowserLogin（上层 fallback 浏览器）

注意：thomsonreuters 用 SAML/SSO，签到表单字段名 / 隐藏 token 可能变。
此实现做防御式解析，所有解析失败统一 raise NeedBrowserLogin。
失败时会把当前 HTML 落盘到 data/http_login_<step>.html 方便调试。
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from script.project_paths import DATA_DIR
from spider.login.browser_login import HOME_URL, USER_AGENT
from spider.login.browser_manager import CF_MARKERS, LOGIN_MARKERS
from spider.paths import COOKIE_FILE, STORAGE_FILE


class NeedBrowserLogin(Exception):
    """HTTP 登录走不通，需要 fallback 到浏览器。"""


_log = logging.getLogger("spider.login.http")


# ==================== 工具 ====================

def _has_marker(html: str, markers: tuple) -> bool:
    head = (html[:5000] or "").lower()
    return any(m in head for m in markers)


def _seems_logged_in(url: str, html: str) -> bool:
    u = (url or "").lower()
    if "signon" in u or "signin" in u:
        return False
    if "westlaw.com" not in u:
        return False
    soup = BeautifulSoup(html[:20000], "html.parser")
    if soup.find("input", attrs={"name": "Username"}) or soup.find("input", attrs={"name": "username"}):
        return False
    return True


def _save_debug(html: str, step: str) -> None:
    try:
        path = DATA_DIR / f"http_login_{step}.html"
        path.write_text(html, encoding="utf-8")
        _log.info(f"[http-login] dumped HTML for debug: {path}")
    except Exception:
        pass


def _parse_first_form(html: str) -> dict:
    """解析第一个 form：返回 action / method / hidden / 关键字段名。"""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        raise NeedBrowserLogin("no <form> on page")

    hidden: dict[str, str] = {}
    for inp in form.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            hidden[name] = inp.get("value", "")

    user_field = None
    pass_field = None
    all_named = []
    for inp in form.find_all("input"):
        n = inp.get("name")
        if not n:
            continue
        all_named.append(n)
        ln = n.lower()
        if user_field is None and ln in ("username", "emailaddress", "email", "user"):
            user_field = n
        elif pass_field is None and ln == "password":
            pass_field = n

    return {
        "action": form.get("action", ""),
        "method": (form.get("method") or "POST").upper(),
        "hidden": hidden,
        "user_field": user_field,
        "pass_field": pass_field,
        "all_fields": all_named,
    }


def _save_cookies_for_playwright(session: cffi_requests.Session) -> int:
    """把 curl_cffi session.cookies 转成 playwright cookie list 格式。"""
    cookies = []
    try:
        items = list(session.cookies.jar) if hasattr(session.cookies, "jar") else list(session.cookies)
    except Exception:
        items = list(session.cookies)

    for c in items:
        # curl_cffi 的 cookie 对象兼容 http.cookiejar.Cookie
        domain = getattr(c, "domain", "") or ""
        cookies.append({
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", "") or "",
            "domain": domain,
            "path": getattr(c, "path", "/") or "/",
            "expires": getattr(c, "expires", None) or -1,
            "httpOnly": False,
            "secure": bool(getattr(c, "secure", False)),
            "sameSite": "Lax",
        })

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    # 写一份最简 storage_state，给浏览器 fallback 启动时复用
    state = {"cookies": cookies, "origins": []}
    STORAGE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return len(cookies)


# ==================== 主流程 ====================

def http_login() -> int:
    """跑完整 HTTP 登录链路。成功返回写入的 cookie 数；失败 raise NeedBrowserLogin。"""
    username = os.getenv("WESTLAW_USERNAME")
    password = os.getenv("WESTLAW_PASSWORD")
    if not username or not password:
        raise NeedBrowserLogin("WESTLAW_USERNAME / WESTLAW_PASSWORD missing")

    session = cffi_requests.Session(impersonate="chrome120")
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
    })

    # ---- step 1: GET HOME → follow redirects to signon ----
    _log.info("[http-login] step 1: GET HOME_URL, follow redirects")
    try:
        resp = session.get(HOME_URL, allow_redirects=True, timeout=30)
    except Exception as e:
        raise NeedBrowserLogin(f"step1 GET failed: {type(e).__name__}: {e}")

    if _has_marker(resp.text, CF_MARKERS):
        _save_debug(resp.text, "step1_cf")
        raise NeedBrowserLogin("CF challenge on initial navigation")
    if _seems_logged_in(resp.url, resp.text):
        _log.info("[http-login] already logged in via existing cookies")
        return _save_cookies_for_playwright(session)

    # ---- step 2: parse signon form ----
    _log.info(f"[http-login] step 2: parse signon form at {resp.url[:120]}")
    try:
        form = _parse_first_form(resp.text)
    except NeedBrowserLogin:
        _save_debug(resp.text, "step2_no_form")
        raise

    if not form["user_field"]:
        _save_debug(resp.text, "step2_no_user_field")
        raise NeedBrowserLogin(
            f"no Username field found (action={form['action']!r}, fields={form['all_fields'][:10]})"
        )

    # ---- step 3: POST username (and password if same form) ----
    data = dict(form["hidden"])
    data[form["user_field"]] = username
    same_page_pass = bool(form["pass_field"])
    if same_page_pass:
        data[form["pass_field"]] = password

    action_url = urljoin(resp.url, form["action"])
    _log.info(f"[http-login] step 3: POST {form['user_field']}{'+password' if same_page_pass else ''} → {action_url[:120]}")
    try:
        resp = session.request(form["method"], action_url, data=data,
                               allow_redirects=True, timeout=30)
    except Exception as e:
        raise NeedBrowserLogin(f"step3 POST failed: {type(e).__name__}: {e}")

    if _has_marker(resp.text, CF_MARKERS):
        _save_debug(resp.text, "step3_cf")
        raise NeedBrowserLogin("CF challenge after username POST")

    # ---- step 4 (optional): stepped form, post password separately ----
    if not same_page_pass and not _seems_logged_in(resp.url, resp.text):
        try:
            form2 = _parse_first_form(resp.text)
        except NeedBrowserLogin:
            _save_debug(resp.text, "step4_no_form")
            raise
        if form2["pass_field"]:
            data2 = dict(form2["hidden"])
            data2[form2["pass_field"]] = password
            # 有些站点把 username 也带在 hidden 里，没带就再放一次
            if form2["user_field"] and form2["user_field"] not in data2:
                data2[form2["user_field"]] = username
            action2 = urljoin(resp.url, form2["action"])
            _log.info(f"[http-login] step 4: POST password → {action2[:120]}")
            try:
                resp = session.request(form2["method"], action2, data=data2,
                                       allow_redirects=True, timeout=30)
            except Exception as e:
                raise NeedBrowserLogin(f"step4 POST failed: {type(e).__name__}: {e}")
            if _has_marker(resp.text, CF_MARKERS):
                _save_debug(resp.text, "step4_cf")
                raise NeedBrowserLogin("CF challenge after password POST")

    # ---- step 5 (optional): Client ID page ----
    if "ClientID" in resp.text or "clientID" in resp.text or "Client ID" in resp.text:
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            form3_el = soup.find("form")
            if form3_el:
                form3 = _parse_first_form(resp.text)
                data3 = dict(form3["hidden"])
                # 选第一个 ClientID option
                sel = soup.find("select", attrs={"name": "ClientID"})
                if sel:
                    opts = sel.find_all("option")
                    if opts:
                        data3["ClientID"] = opts[0].get("value", "")
                action3 = urljoin(resp.url, form3["action"])
                _log.info(f"[http-login] step 5: Client ID continue → {action3[:120]}")
                resp = session.request(form3["method"], action3, data=data3,
                                       allow_redirects=True, timeout=30)
        except Exception as e:
            _log.info(f"[http-login] step 5 skipped: {e}")

    if _has_marker(resp.text, LOGIN_MARKERS):
        _save_debug(resp.text, "final_login_marker")
        raise NeedBrowserLogin("ended on signon page (auth failed?)")
    if not _seems_logged_in(resp.url, resp.text):
        _save_debug(resp.text, "final_not_westlaw")
        raise NeedBrowserLogin(f"final url not westlaw: {resp.url[:160]}")

    n = _save_cookies_for_playwright(session)
    _log.info(f"[http-login] success: {n} cookies → {COOKIE_FILE}")
    return n
