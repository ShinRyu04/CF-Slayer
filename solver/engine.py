"""Camoufox-backed solver. ``Backend`` is the seam for future backends.

One browser per solve: no pooling in v1. Everything here is synchronous; the
FastAPI layer calls it from a worker thread.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .errors import SolveFailed, SolveTimeout

log = logging.getLogger(__name__)

DEFAULT_EXPIRES_IN = 1800

BrowserFactory = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True, slots=True)
class ClearanceResult:
    clearance: str
    user_agent: str
    domain: str
    expires_in: int
    solve_seconds: float


@runtime_checkable
class Backend(Protocol):
    """A backend turns a URL into a ``cf_clearance`` credential.

    Implementations must raise ``SolveTimeout`` / ``SolveFailed`` instead of
    returning placeholder values, so the HTTP layer can map error codes exactly.
    """

    name: str

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult: ...


def domain_of(url: str) -> str:
    """Cache key: lowercase hostname, no port, no scheme."""
    host = urlsplit(url).hostname or ""
    return host.lower().rstrip(".")


def _default_browser_factory(headless: bool) -> BrowserFactory:
    def factory() -> AbstractContextManager[Any]:
        # Delayed import keeps the service importable (and tests runnable)
        # without the 700 MB Camoufox Firefox build present.
        from camoufox.sync_api import Camoufox

        return Camoufox(headless=headless, geoip=False)

    return factory


class CamoufoxBackend:
    name = "camoufox"

    def __init__(
        self,
        *,
        headless: bool = True,
        challenge_timeout: float = 90.0,
        poll_interval: float = 2.0,
        browser_factory: BrowserFactory | None = None,
    ) -> None:
        if challenge_timeout <= 0 or poll_interval <= 0:
            raise ValueError("challenge_timeout and poll_interval must be positive")
        self._challenge_timeout = challenge_timeout
        self._poll_interval = poll_interval
        self._browser_factory = browser_factory or _default_browser_factory(headless)

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        start = time.monotonic()
        deadline = start + timeout
        challenge_deadline = min(deadline, start + self._challenge_timeout)
        domain = domain_of(url)
        if not domain:
            raise SolveFailed(f"cannot derive a domain from {url!r}")

        try:
            with self._browser_factory() as browser:
                context = browser.new_context()
                try:
                    page = context.new_page()
                    goto_ms = int(max(1.0, min(45.0, deadline - time.monotonic())) * 1000)
                    page.goto(url, wait_until="domcontentloaded", timeout=goto_ms)
                    clearance, user_agent, expires_in = self._wait_for_clearance(
                        page, context, challenge_deadline, deadline, url
                    )
                finally:
                    self._close_quietly(context)
        except (SolveTimeout, SolveFailed):
            raise
        except Exception as exc:  # browser crash, navigation failure, bad launch options
            if time.monotonic() >= deadline:
                raise SolveTimeout(f"solve exceeded {timeout:.0f}s for {domain}") from exc
            raise SolveFailed(f"camoufox error: {type(exc).__name__}: {exc}") from exc

        return ClearanceResult(
            clearance=clearance,
            user_agent=user_agent,
            domain=domain,
            expires_in=expires_in,
            solve_seconds=round(time.monotonic() - start, 3),
        )

    def _wait_for_clearance(
        self,
        page: Any,
        context: Any,
        challenge_deadline: float,
        deadline: float,
        url: str,
    ) -> tuple[str, str, int]:
        while True:
            cookie = self._find_clearance(page, context)
            if cookie is not None:
                user_agent = self._read_user_agent(page)
                if user_agent:
                    expires_in = _expires_in(cookie)
                    if expires_in is not None:
                        return str(cookie["value"]), user_agent, expires_in
            now = time.monotonic()
            if now >= challenge_deadline or now >= deadline:
                raise SolveTimeout(
                    f"cf_clearance did not appear within {self._challenge_timeout:.0f}s for {url}"
                )
            page.wait_for_timeout(int(self._poll_interval * 1000))

    def _find_clearance(self, page: Any, context: Any) -> dict[str, Any] | None:
        for cookie in self._cookies(page, context):
            if cookie.get("name") == "cf_clearance" and cookie.get("value"):
                return cookie
        return None

    def _cookies(self, page: Any, context: Any) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        try:
            cookies.extend(context.cookies())
        except Exception as exc:
            log.debug("context.cookies() failed: %s", exc)
        # document.cookie cannot see HttpOnly cookies, but it costs nothing and
        # covers backends/sites that issue cf_clearance without HttpOnly.
        try:
            raw = page.evaluate("() => document.cookie") or ""
            for part in str(raw).split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name:
                    cookies.append({"name": name, "value": value})
        except Exception as exc:
            log.debug("document.cookie read failed: %s", exc)
        return cookies

    def _read_user_agent(self, page: Any) -> str:
        try:
            value = page.evaluate("() => navigator.userAgent")
        except Exception as exc:
            log.debug("navigator.userAgent read failed: %s", exc)
            return ""
        return str(value) if isinstance(value, str) else ""

    @staticmethod
    def _close_quietly(context: Any) -> None:
        try:
            context.close()
        except Exception as exc:
            log.debug("context.close() failed: %s", exc)


def _expires_in(cookie: dict[str, Any]) -> int | None:
    """Seconds of remaining validity; ``None`` means the cookie is already dead."""
    expires = cookie.get("expires")
    if not isinstance(expires, int | float) or expires <= 0:
        return DEFAULT_EXPIRES_IN
    remaining = int(expires - time.time())
    return remaining if remaining > 0 else None
