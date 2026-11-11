"""Shared fixtures. No network, no real browser."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from solver.config import Settings
from solver.engine import ClearanceResult, domain_of
from solver.main import create_app

os.environ.setdefault("CF_SLAYER_AUTH_TOKEN", "test-token")

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeBackend:
    """Stands in for CamoufoxBackend; records calls and can fail on demand."""

    name = "camoufox"

    def __init__(
        self,
        clearance: str = "clearance-token-value",
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) "
            "Gecko/20100101 Firefox/156.0"
        ),
        expires_in: int = 1700,
        solve_seconds: float = 1.25,
    ) -> None:
        self.clearance = clearance
        self.user_agent = user_agent
        self.expires_in = expires_in
        self.solve_seconds = solve_seconds
        self.calls = 0
        self.urls: list[str] = []
        self.error: Exception | None = None

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        self.calls += 1
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return ClearanceResult(
            clearance=self.clearance,
            user_agent=self.user_agent,
            domain=domain_of(url),
            expires_in=self.expires_in,
            solve_seconds=self.solve_seconds,
        )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        auth_token=TOKEN,
        cache_path=str(tmp_path / "cache.sqlite3"),
        ttl_seconds=1800,
        solve_timeout=5.0,
        challenge_timeout=2.0,
        poll_interval=0.01,
        replay_timeout=5.0,
        headless=True,
    )


@pytest.fixture
def api(settings: Settings):
    backend = FakeBackend()
    app = create_app(settings=settings, backend=backend)
    with TestClient(app) as client:
        yield client, backend, app.state.service


@pytest.fixture
def client(api) -> TestClient:
    return api[0]


@pytest.fixture
def backend(api) -> FakeBackend:
    return api[1]


@pytest.fixture
def service(api):
    return api[2]
