"""统一登录入口：HTTP 优先，失败 fallback 浏览器。

  ensure_logged_in() 是面向各 fetcher 的唯一接口；返回 'cache' / 'http' / 'browser'。
  不需要登录态的纯查询命令（--status / --reset-failed 等）不要调它。
"""

from __future__ import annotations

import logging
import time

from spider.login.browser_login import COOKIE_TTL
from spider.paths import COOKIE_FILE


_log = logging.getLogger("spider.login")


def cookies_valid() -> bool:
    if not COOKIE_FILE.exists():
        return False
    age = time.time() - COOKIE_FILE.stat().st_mtime
    if age > COOKIE_TTL:
        _log.info(f"[login] cookies stale (age={int(age)}s > TTL={COOKIE_TTL}s)")
        return False
    return True


def ensure_logged_in(*, force: bool = False) -> str:
    """确保 cookies 是新鲜的。优先 HTTP 登录；失败 fallback 浏览器。

    Args:
        force: True 时忽略缓存强制重登

    Returns:
        'cache' (用了缓存) / 'http' (HTTP 登录成功) / 'browser' (浏览器 fallback)
    """
    if not force and cookies_valid():
        _log.info("[login] using cached cookies")
        return "cache"

    # 先试 HTTP（不启 chromium）
    try:
        from spider.login.http_login import NeedBrowserLogin, http_login
        http_login()
        return "http"
    except NeedBrowserLogin as e:
        _log.warning(f"[login] HTTP login signaled need-browser: {e}")
    except Exception as e:
        _log.warning(f"[login] HTTP login error: {type(e).__name__}: {e}")

    # Fallback：启一次性浏览器跑完登录
    _log.info("[login] falling back to browser login (chromium)")
    from spider.login.browser_login import browser_login_once
    browser_login_once()
    return "browser"
