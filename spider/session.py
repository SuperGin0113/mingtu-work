"""curl_cffi session 构造 + Westlaw cookie 域名白名单。各 fetcher 共用。"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from curl_cffi import requests as cffi_requests

from spider.paths import COOKIE_FILE

ALLOWED_COOKIE_DOMAINS: tuple[str, ...] = (
    ".westlaw.com", "westlaw.com",
    ".thomsonreuters.com", "thomsonreuters.com",
    ".next.westlaw.com", "next.westlaw.com",
    "1.next.westlaw.com",
    ".1.next.westlaw.com",
    ".i1.next.westlaw.com",
    ".c1.next.westlaw.com",
    "signon.thomsonreuters.com",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _domain_allowed(domain: str, allowed: Iterable[str]) -> bool:
    domain = (domain or "").lower()
    return any(domain == d or domain.endswith(d) for d in allowed)


def make_session(
    accept: str,
    referer: str,
    *,
    extra_headers: dict[str, str] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    impersonate: str = "chrome120",
    cookie_file=COOKIE_FILE,
    allowed_domains: Iterable[str] = ALLOWED_COOKIE_DOMAINS,
    logger: logging.Logger | None = None,
) -> cffi_requests.Session:
    """读 COOKIE_FILE → 构造 curl_cffi Session（指纹 + cookies + headers）。"""
    log = logger or logging.getLogger("spider.session")

    with open(cookie_file, "r", encoding="utf-8-sig") as f:
        cookies = json.load(f)

    session = cffi_requests.Session(impersonate=impersonate)
    injected = 0
    for c in cookies:
        if not _domain_allowed(c.get("domain", ""), allowed_domains):
            continue
        try:
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"), path=c.get("path", "/"),
            )
            injected += 1
        except Exception:
            pass
    log.info(f"[session] injected {injected}/{len(cookies)} cookies (impersonate={impersonate})")

    headers = {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "User-Agent": user_agent,
    }
    if extra_headers:
        headers.update(extra_headers)
    session.headers.update(headers)
    return session
