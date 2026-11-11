from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from solver.chain import BackendChain, ChainSolveFailed, ChainSolveTimeout
from solver.config import Settings
from solver.engine import Attempt, Backend, ClearanceResult
from solver.errors import SolveFailed, SolveTimeout
from solver.main import create_app

TOKEN = "chain-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "https://target.example/"


def make_result(clearance: str = "value") -> ClearanceResult:
    return ClearanceResult(
        clearance=clearance,
        user_agent="Mozilla/5.0 test",
        domain="target.example",
        expires_in=1700,
        solve_seconds=1.0,
    )


class StubBackend:
    def __init__(
        self,
        name: str,
        *,
        result: ClearanceResult | None = None,
        error: Exception | None = None,
        available: tuple[bool, str] | None = None,
        available_error: Exception | None = None,
    ) -> None:
        self.name = name
        self._result = result or make_result()
        self._error = error
        self._available = available
        self._available_error = available_error
        self.calls = 0
        self.timeouts: list[float] = []

    def available(self) -> tuple[bool, str]:
        if self._available_error is not None:
            raise self._available_error
        return self._available if self._available is not None else (True, "")

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        self.calls += 1
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        return self._result


def test_first_success_short_circuits():
    first = StubBackend("camoufox")
    second = StubBackend("pydoll")
    chain = BackendChain([first, second])

    result = chain.solve(URL, timeout=42.0)

    assert result.clearance == first._result.clearance
    assert [a.backend for a in result.attempts] == ["camoufox"]
    assert result.attempts[0].ok is True
    assert result.attempts[0].error is None
    assert first.calls == 1
    assert second.calls == 0
    assert first.timeouts == [42.0]


def test_order_is_honored_and_failure_falls_through():
    first = StubBackend("camoufox", error=SolveFailed("blocked"))
    second = StubBackend("pydoll", result=make_result("pydoll-value"))
    chain = BackendChain([first, second])

    result = chain.solve(URL, timeout=5.0)

    assert result.clearance == "pydoll-value"
    assert [a.backend for a in result.attempts] == ["camoufox", "pydoll"]
    assert [a.ok for a in result.attempts] == [False, True]
    assert result.attempts[0].error == "blocked"
    assert result.attempts[1].error is None
    assert all(a.seconds >= 0 for a in result.attempts)


def test_all_fail_aggregates_attempts():
    chain = BackendChain(
        [
            StubBackend("camoufox", error=SolveFailed("no cookie")),
            StubBackend("pydoll", error=SolveFailed("chrome crashed")),
        ]
    )

    with pytest.raises(ChainSolveFailed) as info:
        chain.solve(URL, timeout=5.0)

    error = info.value
    assert isinstance(error, SolveFailed)
    assert [a.backend for a in error.attempts] == ["camoufox", "pydoll"]
    assert all(a.ok is False for a in error.attempts)
    assert "camoufox: no cookie" in str(error)
    assert "pydoll: chrome crashed" in str(error)


def test_all_timeout_raises_timeout_with_attempts():
    chain = BackendChain(
        [
            StubBackend("camoufox", error=SolveTimeout("t1")),
            StubBackend("pydoll", error=SolveTimeout("t2")),
        ]
    )

    with pytest.raises(ChainSolveTimeout) as info:
        chain.solve(URL, timeout=1.0)

    assert isinstance(info.value, SolveTimeout)
    assert len(info.value.attempts) == 2


def test_mixed_timeout_and_failure_is_not_a_timeout():
    chain = BackendChain(
        [
            StubBackend("camoufox", error=SolveTimeout("t1")),
            StubBackend("pydoll", error=SolveFailed("boom")),
        ]
    )

    with pytest.raises(ChainSolveFailed):
        chain.solve(URL, timeout=1.0)


def test_unavailable_backend_is_skipped_without_calling_solve():
    unavailable = StubBackend("pydoll", available=(False, "Chrome/Chromium not found"))
    fallback = StubBackend("camoufox")
    chain = BackendChain([unavailable, fallback])

    result = chain.solve(URL, timeout=5.0)

    assert unavailable.calls == 0
    assert fallback.calls == 1
    assert result.attempts[0].backend == "pydoll"
    assert result.attempts[0].ok is False
    assert "Chrome/Chromium not found" in str(result.attempts[0].error)
    assert result.attempts[0].seconds == 0.0


def test_availability_probe_exception_is_contained():
    broken = StubBackend("pydoll", available_error=RuntimeError("probe exploded"))
    fallback = StubBackend("camoufox")
    chain = BackendChain([broken, fallback])

    result = chain.solve(URL, timeout=5.0)

    assert result.attempts[0].ok is False
    assert "probe exploded" in str(result.attempts[0].error)
    assert broken.calls == 0


def test_unexpected_backend_exception_falls_through():
    crashing = StubBackend("camoufox", error=RuntimeError("segfault"))
    fallback = StubBackend("pydoll")
    chain = BackendChain([crashing, fallback])

    result = chain.solve(URL, timeout=5.0)

    assert result.attempts[0].error == "RuntimeError: segfault"
    assert fallback.calls == 1


def test_chain_satisfies_backend_protocol_and_uses_first_name():
    chain = BackendChain([StubBackend("camoufox"), StubBackend("pydoll")])
    assert isinstance(chain, Backend)
    assert chain.name == "camoufox"


def test_empty_chain_is_rejected():
    with pytest.raises(ValueError, match="at least one backend"):
        BackendChain([])


def test_attempt_dict_shape():
    attempt = Attempt(backend="pydoll", ok=False, error="nope", seconds=1.23456)
    assert attempt.as_dict() == {
        "backend": "pydoll",
        "ok": False,
        "error": "nope",
        "seconds": 1.235,
    }


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        auth_token=TOKEN,
        cache_path=str(tmp_path / "cache.sqlite3"),
        solve_timeout=5.0,
        backends="camoufox,pydoll",
        prewarm_domains="",
    )


def test_solve_response_includes_attempt_trace(tmp_path):
    chain = BackendChain([StubBackend("camoufox")])
    app = create_app(settings=_settings(tmp_path), backend=chain)
    with TestClient(app) as client:
        response = client.post("/solve", json={"url": URL}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["attempts"] == [
        {"backend": "camoufox", "ok": True, "error": None, "seconds": 0.0}
    ]


def test_cached_solve_returns_empty_attempt_trace(tmp_path):
    chain = BackendChain([StubBackend("camoufox")])
    app = create_app(settings=_settings(tmp_path), backend=chain)
    with TestClient(app) as client:
        client.post("/solve", json={"url": URL}, headers=AUTH)
        second = client.post("/solve", json={"url": URL}, headers=AUTH)
    assert second.json()["cached"] is True
    assert second.json()["attempts"] == []


def test_pydoll_only_without_chrome_returns_clear_error(tmp_path):
    settings = _settings(tmp_path)
    settings.backends = "pydoll"
    settings.chrome_path = str(tmp_path / "no-such-chrome.exe")
    app = create_app(settings=settings)  # must not crash at startup

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/solve", json={"url": URL}, headers=AUTH)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "SOLVE_FAILED"
    assert error["attempts"][0]["backend"] == "pydoll"
    assert "Chrome" in error["message"]
    assert "CF_SLAYER_CHROME_PATH" in error["attempts"][0]["error"]
