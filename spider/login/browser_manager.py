"""常驻 playwright 浏览器（lazy 启动），用于 CF challenge / 登录刷新 / cookie 同步。"""

from __future__ import annotations

import json
import logging
import time

from playwright.sync_api import sync_playwright

from spider.login.browser_login import get_authenticated_context
from spider.paths import COOKIE_FILE, STORAGE_FILE

BASE_URL = "https://1.next.westlaw.com"

CF_MARKERS = (
    "cf-browser-verification", "challenge-platform", "cf-chl-bypass",
    "just a moment", "checking your browser",
)

LOGIN_MARKERS = (
    "signon.thomsonreuters", "cosi/signon", "sessionexpired",
    "please sign in", "your session has expired", "loginform",
    "productid=cbt", "redirectto",
)


class BrowserManager:
    """lazy 启动：只有真正需要兜底（CF / 登录失效）时才打开 chromium。

    每个 fetcher 自带 logger，传进来共用一份日志通道（保证日志落入对应文件）。
    """

    def __init__(self, logger: logging.Logger | None = None):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.logger = logger or logging.getLogger("spider.browser_manager")

    def is_running(self) -> bool:
        return self.browser is not None

    def start(self) -> None:
        if self.is_running():
            return
        self.logger.info("[browser] starting playwright browser (lazy)...")
        self.pw = sync_playwright().start()
        self.browser, self.context, self.page = get_authenticated_context(self.pw)
        self._save_cookies()
        self.logger.info("[browser] browser ready and kept alive")

    def _save_cookies(self) -> None:
        cookies = self.context.cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        self.context.storage_state(path=str(STORAGE_FILE))
        self.logger.info(f"[browser] cookies saved ({len(cookies)} cookies)")

    def solve_challenge(self, url: str) -> str | None:
        """访问触发 CF 验证的 URL，等通过后返回页面 HTML；失败返回 None。"""
        self.start()
        self.logger.info(f"[browser] navigating to solve challenge: {url[:100]}...")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            for attempt in range(30):
                time.sleep(3)
                title = self.page.title() or ""
                content = self.page.content()
                head = content[:5000].lower()
                has_challenge = any(m in head for m in CF_MARKERS)
                if not has_challenge and len(content) > 1000:
                    self.logger.info(
                        f"[browser] challenge solved after {(attempt + 1) * 3}s, title: {title}"
                    )
                    self._save_cookies()
                    return content
                if attempt % 5 == 4:
                    self.logger.info(
                        f"[browser] still waiting for challenge... ({(attempt + 1) * 3}s)"
                    )
            self.logger.info("[browser] challenge timeout after 90s")
            self._save_cookies()
            return None
        except Exception as e:
            self.logger.info(f"[browser] solve_challenge error: {e}")
            self._save_cookies()
            return None

    def refresh_login(self) -> None:
        """登录失效时调用：访问首页探测，必要时重启浏览器走完整登录流。"""
        self.start()
        self.logger.info("[browser] refreshing login...")
        try:
            self.page.goto(
                f"{BASE_URL}/Search/Home.html?transitionType=Default&contextData=(sc.Default)",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            time.sleep(3)
            head = self.page.content()[:5000].lower()
            if "signon" in head or "loginform" in head:
                self.close()
                self.start()
            else:
                self._save_cookies()
                self.logger.info("[browser] login still valid, cookies refreshed")
        except Exception as e:
            self.logger.info(f"[browser] refresh_login error: {e}, restarting...")
            self.close()
            self.start()

    def close(self) -> None:
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
        self.logger.info("[browser] closed")
