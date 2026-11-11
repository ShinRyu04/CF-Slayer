"""Pydoll (CDP) backend: drives an installed Chrome/Chromium directly.

Pydoll talks to Chrome over the DevTools protocol with no WebDriver. The browser
is detected at solve time; if it is absent the chain skips this backend instead
of failing the request.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .engine import ClearanceResult, domain_of
from .errors import SolveFailed, SolveTimeout

log = logging.getLogger(__name__)

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
CHROME_WHICH_NAMES = ("chrome", "chrome.exe", "chromium", "chromium.exe", "google-chrome")


def find_chrome(explicit: str | None = None) -> str | None:
    """Return a usable Chrome/Chromium path, or ``None``.

    ``explicit`` (CF_SLAYER_CHROME_PATH) wins; when it points at nothing the
    lookup does not silently fall back — that would hide a typo.
    """
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in CHROME_WHICH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def chrome_major_version(chrome_path: str) -> str | None:
    """Chrome stores its build under ``Application/<version>/`` on Windows."""
    match = re.match(r"(\d+)\.", Path(chrome_path).parent.name)
    return match.group(1) if match else None


def chrome_user_agent(chrome_path: str) -> str | None:
    """A non-headless UA matching the installed build.

    Chrome advertises ``HeadlessChrome`` by default, which is an instant
    Cloudflare signal. ``--user-agent`` makes navigator.userAgent consistent
    with the network header too.
    """
    major = chrome_major_version(chrome_path)
    if major is None:
        return None
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


class PydollBackend:
    name = "pydoll"

    def __init__(
        self,
        *,
        headless: bool = True,
        challenge_timeout: float = 90.0,
        poll_interval: float = 2.0,
        chrome_path: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if challenge_timeout <= 0 or poll_interval <= 0:
            raise ValueError("challenge_timeout and poll_interval must be positive")
        self._headless = headless
        self._challenge_timeout = challenge_timeout
        self._poll_interval = poll_interval
        self._chrome_path = chrome_path
        self._user_agent = user_agent

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("pydoll") is None:
            return False, "pydoll not installed (pip install pydoll-python)"
        path = self._resolve_chrome()
        if path is None:
            if self._chrome_path:
                return False, f"Chrome not found at CF_SLAYER_CHROME_PATH={self._chrome_path!r}"
            return False, "Chrome/Chromium not found (common install paths and PATH)"
        return True, ""

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        ok, reason = self.available()
        if not ok:
            raise SolveFailed(reason)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SolveFailed("pydoll backend must run in a worker thread (event loop detected)")

        start = time.monotonic()
        try:
            clearance, user_agent, expires_in = asyncio.run(
                self._solve_async(url, timeout, start)
            )
        except (SolveFailed, SolveTimeout):
            raise
        except Exception as exc:
            raise SolveFailed(f"pydoll error: {type(exc).__name__}: {exc}") from exc
        return ClearanceResult(
            clearance=clearance,
            user_agent=user_agent,
            domain=domain_of(url),
            expires_in=expires_in,
            solve_seconds=round(time.monotonic() - start, 3),
        )

    def _resolve_chrome(self) -> str | None:
        return find_chrome(self._chrome_path)

    async def _solve_async(
        self, url: str, timeout: float, start: float
    ) -> tuple[str, str, int]:
        from pydoll.browser import Chrome
        from pydoll.browser.options import ChromiumOptions

        chrome_path = self._resolve_chrome()
        if chrome_path is None:
            raise SolveFailed("Chrome/Chromium not found")
        deadline = start + min(timeout, self._challenge_timeout)

        options = ChromiumOptions()
        options.headless = self._headless
        options.binary_location = chrome_path
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-blink-features=AutomationControlled")
        user_agent = self._user_agent or chrome_user_agent(chrome_path)
        if user_agent:
            options.add_argument(f"--user-agent={user_agent}")

        async with Chrome(options=options) as browser:
            tab = await browser.start()
            goto_timeout = int(max(1.0, min(45.0, deadline - time.monotonic())))
            await tab.go_to(url, timeout=goto_timeout)
            return await self._poll_clearance(tab, deadline, url)

    async def _poll_clearance(
        self, tab: Any, deadline: float, url: str
    ) -> tuple[str, str, int]:
        while True:
            cookie = await self._find_cookie(tab)
            if cookie is not None:
                user_agent = await self._read_user_agent(tab)
                if user_agent:
                    return str(cookie["value"]), user_agent, _expires_in(cookie)
            if time.monotonic() >= deadline:
                raise SolveTimeout(
                    f"cf_clearance did not appear within {self._challenge_timeout:.0f}s for {url}"
                )
            await asyncio.sleep(self._poll_interval)

    async def _find_cookie(self, tab: Any) -> dict[str, Any] | None:
        try:
            cookies = await tab.get_cookies()
        except Exception as exc:  # tab may be mid-navigation
            log.debug("pydoll get_cookies failed: %s", exc)
            return None
        for cookie in cookies or ():
            if cookie.get("name") == "cf_clearance" and cookie.get("value"):
                return dict(cookie)
        return None

    async def _read_user_agent(self, tab: Any) -> str:
        try:
            response = await tab.execute_script("navigator.userAgent", return_by_value=True)
        except Exception as exc:  # execution context can be destroyed
            log.debug("pydoll userAgent read failed: %s", exc)
            return ""
        value = response.get("result", {}).get("result", {}).get("value")
        return value if isinstance(value, str) else ""


def _expires_in(cookie: dict[str, Any]) -> int:
    expires = cookie.get("expires")
    if not isinstance(expires, int | float) or expires <= 0:
        return 1800
    remaining = int(expires - time.time())
    return remaining if remaining > 0 else 1800
