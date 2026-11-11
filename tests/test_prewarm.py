from __future__ import annotations

import asyncio
import threading

import pytest

from solver.cache import SQLiteCache
from solver.config import Settings
from solver.engine import ClearanceResult
from solver.errors import SolveFailed
from solver.main import Service
from solver.prewarm import PrewarmWorker, normalize_target

URL = "https://target.example/"
DOMAIN = "target.example"


class StubBackend:
    def __init__(self, error: Exception | None = None) -> None:
        self.name = "stub"
        self.calls = 0
        self._error = error

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ClearanceResult(
            clearance="value",
            user_agent="Mozilla/5.0 test",
            domain=DOMAIN,
            expires_in=1700,
            solve_seconds=0.5,
        )


class BlockingBackend:
    name = "blocking"

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10)
        return ClearanceResult(
            clearance="value",
            user_agent="Mozilla/5.0 test",
            domain=DOMAIN,
            expires_in=1700,
            solve_seconds=0.5,
        )


def _make(tmp_path, backend, *, margin: int = 300, now=lambda: 1000.0):
    settings = Settings(
        _env_file=None,
        auth_token="prewarm-token",
        cache_path=str(tmp_path / "cache.sqlite3"),
        ttl_seconds=1800,
    )
    cache = SQLiteCache(settings.cache_path, settings.ttl_seconds)
    service = Service(settings, cache, backend)
    worker = PrewarmWorker(service, [URL], margin_seconds=margin, interval=0.01, now=now)
    return service, cache, worker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("steamdb.info", "https://steamdb.info/"),
        ("steamdb.info/", "https://steamdb.info/"),
        ("https://steamdb.info", "https://steamdb.info"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/"),
        ("  example.com  ", "https://example.com/"),
    ],
)
def test_normalize_target(raw, expected):
    assert normalize_target(raw) == expected


def test_fresh_entry_is_skipped(tmp_path):
    backend = StubBackend()
    service, cache, worker = _make(tmp_path, backend)
    cache.put(DOMAIN, "old", "Mozilla/5.0", ttl=1800, now=1000.0)

    asyncio.run(worker.tick())

    assert backend.calls == 0
    assert service.stats.snapshot()["solves"] == 0
    cache.close()


def test_entry_inside_margin_is_renewed(tmp_path):
    backend = StubBackend()
    service, cache, worker = _make(tmp_path, backend, now=lambda: 1000.0 + 1600)
    cache.put(DOMAIN, "old", "Mozilla/5.0", ttl=1800, now=1000.0)

    asyncio.run(worker.tick())

    assert backend.calls == 1
    assert service.stats.snapshot()["solves"] == 1
    updated = cache.get(DOMAIN, now=1000.0 + 1601)
    assert updated is not None and updated.clearance == "value"
    cache.close()


def test_missing_entry_is_solved(tmp_path):
    backend = StubBackend()
    _, cache, worker = _make(tmp_path, backend)

    asyncio.run(worker.tick())

    assert backend.calls == 1
    assert cache.get(DOMAIN) is not None
    cache.close()


def test_error_is_counted_not_raised(tmp_path):
    backend = StubBackend(error=SolveFailed("blocked"))
    service, cache, worker = _make(tmp_path, backend)

    asyncio.run(worker.tick())

    stats = service.stats.snapshot()
    assert stats["prewarm_errors"] == 1
    assert stats["solves"] == 0
    assert cache.get(DOMAIN) is None
    cache.close()


def test_concurrent_ticks_do_not_duplicate_solves(tmp_path):
    backend = BlockingBackend()
    _, cache, worker = _make(tmp_path, backend)

    async def scenario() -> None:
        first = asyncio.create_task(worker.tick())
        while not backend.started.is_set():
            await asyncio.sleep(0.005)
        second = asyncio.create_task(worker.tick())
        await asyncio.sleep(0.02)
        assert backend.calls == 1
        backend.release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert backend.calls == 1
    cache.close()


def test_run_exits_when_stop_is_set(tmp_path):
    backend = StubBackend()
    _, cache, worker = _make(tmp_path, backend)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        while backend.calls == 0:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    cache.close()
