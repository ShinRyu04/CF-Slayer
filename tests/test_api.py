from __future__ import annotations

import solver.main as main_module
from client.replay import ReplayResult
from solver.engine import ClearanceResult, domain_of
from solver.errors import SolveFailed, SolveTimeout
from tests.conftest import AUTH

TARGET = "https://target.example/page"


def test_health_is_open(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"state": "ready", "browser": "camoufox"}


def test_solve_requires_bearer_token(client):
    response = client.post("/solve", json={"url": TARGET})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING"


def test_solve_rejects_wrong_token(client):
    response = client.post(
        "/solve", json={"url": TARGET}, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING"


def test_solve_rejects_non_http_url(client):
    response = client.post("/solve", json={"url": "ftp://target.example"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_solve_rejects_missing_url(client):
    response = client.post("/solve", json={}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_solve_rejects_unknown_want(client):
    response = client.post(
        "/solve", json={"url": TARGET, "want": "turnstile"}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_solve_success_then_cache_hit(client, backend):
    first = client.post("/solve", json={"url": TARGET}, headers=AUTH)
    assert first.status_code == 200
    body = first.json()
    assert body["clearance"] == backend.clearance
    assert body["user_agent"] == backend.user_agent
    assert body["domain"] == "target.example"
    assert body["cached"] is False
    assert body["solve_seconds"] == 1.25
    assert 1 <= body["expires_in"] <= 1700

    second = client.post("/solve", json={"url": TARGET}, headers=AUTH)
    assert second.status_code == 200
    cached_body = second.json()
    assert cached_body["cached"] is True
    assert cached_body["clearance"] == body["clearance"]
    assert cached_body["solve_seconds"] == 0.0
    assert backend.calls == 1


def test_solve_timeout_maps_to_504(client, backend):
    backend.error = SolveTimeout("no cookie")
    response = client.post("/solve", json={"url": TARGET}, headers=AUTH)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "SOLVE_TIMEOUT"


def test_solve_failure_maps_to_502(client, backend):
    backend.error = SolveFailed("challenge not solved")
    response = client.post("/solve", json={"url": TARGET}, headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SOLVE_FAILED"


def test_stats_requires_auth(client):
    assert client.get("/stats").status_code == 401


def test_stats_tracks_hits_solves_and_errors(client, backend):
    client.post("/solve", json={"url": TARGET}, headers=AUTH)
    client.post("/solve", json={"url": TARGET}, headers=AUTH)
    backend.error = SolveFailed("boom")
    client.post("/solve", json={"url": "https://other.example"}, headers=AUTH)

    stats = client.get("/stats", headers=AUTH).json()
    assert stats["solves"] == 1
    assert stats["cache_hits"] == 1
    assert stats["errors"] == 1
    assert stats["avg_solve_seconds"] == 1.25
    assert stats["cached_domains"] == 1


def _fresh_solve(client) -> None:
    response = client.post("/solve", json={"url": TARGET}, headers=AUTH)
    assert response.status_code == 200, response.text


def test_replay_requires_token(client):
    assert client.get("/replay", params={"url": TARGET}).status_code == 401


def test_replay_uses_cached_credentials(client, monkeypatch):
    _fresh_solve(client)
    seen: dict[str, object] = {}

    def fake_replay(url, clearance, user_agent, domain="", *, timeout=30.0):
        seen.update(
            url=url, clearance=clearance, user_agent=user_agent, domain=domain, timeout=timeout
        )
        return ReplayResult(
            status=200, length=4321, challenge_page=False, elapsed_seconds=0.4, url=url
        )

    monkeypatch.setattr(main_module, "replay", fake_replay)

    response = client.get("/replay", params={"url": TARGET}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 200
    assert body["length"] == 4321
    assert body["challenge_page"] is False
    assert body["resolved"] is False
    assert seen["clearance"] == "clearance-token-value"
    assert seen["domain"] == "target.example"
    assert "Firefox" in str(seen["user_agent"])


def test_replay_403_triggers_one_resolve(client, backend, monkeypatch):
    calls: list[str] = []

    def fake_replay(url, clearance, user_agent, domain="", *, timeout=30.0):
        calls.append(clearance)
        challenged = len(calls) == 1
        return ReplayResult(
            status=403 if challenged else 200,
            length=512 if challenged else 9999,
            challenge_page=challenged,
            elapsed_seconds=0.1,
            url=url,
        )

    monkeypatch.setattr(main_module, "replay", fake_replay)

    response = client.get("/replay", params={"url": TARGET}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 200
    assert body["length"] == 9999
    assert body["resolved"] is True
    assert backend.calls == 2
    assert len(calls) == 2


def test_replay_still_403_after_resolve_maps_to_upstream_403(client, backend, monkeypatch):
    def fake_replay(url, clearance, user_agent, domain="", *, timeout=30.0):
        return ReplayResult(
            status=403, length=512, challenge_page=True, elapsed_seconds=0.1, url=url
        )

    monkeypatch.setattr(main_module, "replay", fake_replay)

    response = client.get("/replay", params={"url": TARGET}, headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_403"
    assert backend.calls == 2


def test_engine_result_domain_is_normalized(service):
    result = service.backend.solve("https://UPPER.Example/", timeout=1.0)
    assert result.domain == domain_of("https://UPPER.Example/") == "upper.example"
    assert isinstance(result, ClearanceResult)
