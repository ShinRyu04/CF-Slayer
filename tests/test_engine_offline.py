from __future__ import annotations

import time
from typing import Self

import pytest

from solver.engine import Backend, CamoufoxBackend, domain_of
from solver.errors import SolveFailed, SolveTimeout

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self._context = context

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self._context.goto_calls.append((url, wait_until, timeout))
        if self._context.raise_on_goto is not None:
            raise self._context.raise_on_goto

    def wait_for_timeout(self, milliseconds: int) -> None:
        self._context.polls += 1

    def evaluate(self, script: str) -> str:
        if "document.cookie" in script:
            return ""
        if "userAgent" in script:
            return self._context.user_agent
        raise AssertionError(f"unexpected script: {script}")


class FakeContext:
    def __init__(
        self,
        *,
        polls_before_cookie: int,
        user_agent: str = UA,
        expires_in: float = 1200.0,
        raise_on_goto: Exception | None = None,
    ) -> None:
        self.polls_before_cookie = polls_before_cookie
        self.user_agent = user_agent
        self.expires_in = expires_in
        self.raise_on_goto = raise_on_goto
        self.polls = 0
        self.closed = False
        self.goto_calls: list[tuple[str, str | None, int | None]] = []

    def new_page(self) -> FakePage:
        return FakePage(self)

    def cookies(self) -> list[dict[str, object]]:
        if self.raise_on_goto is not None:
            return []
        if self.polls < self.polls_before_cookie:
            return []
        return [
            {
                "name": "cf_clearance",
                "value": "fake-clearance",
                "expires": time.time() + self.expires_in,
            }
        ]

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def new_context(self) -> FakeContext:
        return self.context


def make_backend(
    context: FakeContext, *, challenge_timeout: float = 5.0, poll_interval: float = 0.001
) -> tuple[CamoufoxBackend, FakeBrowser]:
    browser = FakeBrowser(context)
    backend = CamoufoxBackend(
        challenge_timeout=challenge_timeout,
        poll_interval=poll_interval,
        browser_factory=lambda: browser,
    )
    return backend, browser


def test_solve_collects_cookie_and_user_agent():
    context = FakeContext(polls_before_cookie=2)
    backend, browser = make_backend(context)

    result = backend.solve("https://target.example/path?q=1", timeout=5.0)

    assert result.clearance == "fake-clearance"
    assert result.user_agent == UA
    assert result.domain == "target.example"
    assert 1190 <= result.expires_in <= 1200
    assert result.solve_seconds >= 0.0
    assert context.polls == 2
    assert context.closed is True
    assert browser.closed is True
    assert context.goto_calls[0][0] == "https://target.example/path?q=1"
    assert context.goto_calls[0][1] == "domcontentloaded"


def test_solve_times_out_without_cookie():
    context = FakeContext(polls_before_cookie=10**9)
    backend, browser = make_backend(context, challenge_timeout=0.05, poll_interval=0.005)

    with pytest.raises(SolveTimeout):
        backend.solve("https://target.example", timeout=5.0)

    assert context.closed is True
    assert browser.closed is True


def test_goto_failure_is_reported_as_solve_failed():
    context = FakeContext(polls_before_cookie=0, raise_on_goto=RuntimeError("net::ERR_FAILED"))
    backend, _ = make_backend(context)

    with pytest.raises(SolveFailed, match="net::ERR_FAILED"):
        backend.solve("https://target.example", timeout=5.0)

    assert context.closed is True


def test_browser_launch_failure_is_reported_as_solve_failed():
    def exploding_factory():
        raise RuntimeError("firefox binary missing")

    backend = CamoufoxBackend(browser_factory=exploding_factory)
    with pytest.raises(SolveFailed, match="firefox binary missing"):
        backend.solve("https://target.example", timeout=5.0)


def test_missing_user_agent_blocks_the_credential():
    context = FakeContext(polls_before_cookie=0, user_agent="")
    backend, _ = make_backend(context, challenge_timeout=0.02, poll_interval=0.005)

    with pytest.raises(SolveTimeout):
        backend.solve("https://target.example", timeout=5.0)


def test_domain_of_normalizes_host():
    assert domain_of("https://Target.Example:8443/a") == "target.example"
    assert domain_of("http://sub.target.example/") == "sub.target.example"
    assert domain_of("not a url") == ""


def test_backend_protocol_is_satisfied():
    backend, _ = make_backend(FakeContext(polls_before_cookie=0))
    assert isinstance(backend, Backend)
    assert backend.name == "camoufox"
