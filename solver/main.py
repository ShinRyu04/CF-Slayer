"""FastAPI service: /solve, /replay, /health, /stats."""

from __future__ import annotations

import functools
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

import anyio
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from client.replay import ReplayResult, replay

from . import __version__
from .cache import CachedClearance, SQLiteCache
from .config import Settings, load_settings
from .engine import Backend, CamoufoxBackend, ClearanceResult, domain_of
from .errors import (
    AUTH_MISSING,
    BAD_REQUEST,
    SOLVE_FAILED,
    SOLVE_TIMEOUT,
    UPSTREAM_403,
    ApiError,
    SolveFailed,
    SolveTimeout,
)

log = logging.getLogger(__name__)


class SolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    want: str = "clearance"


@dataclass
class Stats:
    cache_hits: int = 0
    solves: int = 0
    errors: int = 0
    total_solve_seconds: float = 0.0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def record_solve(self, seconds: float) -> None:
        with self._lock:
            self.solves += 1
            self.total_solve_seconds += max(0.0, seconds)

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            avg = self.total_solve_seconds / self.solves if self.solves else 0.0
            return {
                "cache_hits": self.cache_hits,
                "solves": self.solves,
                "errors": self.errors,
                "avg_solve_seconds": round(avg, 2),
                "uptime_seconds": round(time.time() - self.started_at, 1),
            }


class Service:
    def __init__(self, settings: Settings, cache: SQLiteCache, backend: Backend) -> None:
        self.settings = settings
        self.cache = cache
        self.backend = backend
        self.stats = Stats()

    async def solve(self, url: str) -> dict[str, Any]:
        domain = domain_of(url)
        cached = self.cache.get(domain)
        if cached is not None:
            self.stats.record_hit()
            return self._cached_response(cached)

        result = await self._run_backend(url)
        cached = self._store(domain, result)
        return {
            "clearance": cached.clearance,
            "user_agent": cached.user_agent,
            "domain": domain,
            "expires_in": cached.remaining(),
            "cached": False,
            "solve_seconds": round(result.solve_seconds, 2),
        }

    async def replay(self, url: str) -> dict[str, Any]:
        domain = domain_of(url)
        cached = self.cache.get(domain)
        resolved = False
        if cached is None:
            cached = await self._solve_and_store(url)
            resolved = True

        result = await self._replay_once(url, cached, domain)
        if result.challenge_page:
            # Credential died (TTL rolled over, IP drifted, or UA mismatch):
            # drop it and re-solve exactly once before giving up.
            log.info("replay challenged for %s; re-solving once", domain)
            self.cache.invalidate(domain)
            cached = await self._solve_and_store(url)
            result = await self._replay_once(url, cached, domain)
            resolved = True
            if result.challenge_page:
                self.stats.record_error()
                raise ApiError(
                    UPSTREAM_403,
                    f"target still returned a challenge after re-solve "
                    f"(status {result.status}, {result.length} bytes)",
                )

        return {
            "url": result.url,
            "domain": domain,
            "status": result.status,
            "length": result.length,
            "challenge_page": result.challenge_page,
            "replay_seconds": result.elapsed_seconds,
            "resolved": resolved,
        }

    def _cached_response(self, cached: CachedClearance) -> dict[str, Any]:
        return {
            "clearance": cached.clearance,
            "user_agent": cached.user_agent,
            "domain": cached.domain,
            "expires_in": cached.remaining(),
            "cached": True,
            "solve_seconds": 0.0,
        }

    async def _run_backend(self, url: str) -> ClearanceResult:
        try:
            result = await anyio.to_thread.run_sync(
                self.backend.solve, url, self.settings.solve_timeout
            )
        except SolveTimeout as exc:
            self.stats.record_error()
            raise ApiError(SOLVE_TIMEOUT, str(exc)) from exc
        except SolveFailed as exc:
            self.stats.record_error()
            raise ApiError(SOLVE_FAILED, str(exc)) from exc
        except Exception as exc:
            self.stats.record_error()
            log.exception("backend raised an unexpected error")
            raise ApiError(SOLVE_FAILED, f"backend error: {exc}") from exc
        self.stats.record_solve(result.solve_seconds)
        return result

    async def _solve_and_store(self, url: str) -> CachedClearance:
        result = await self._run_backend(url)
        return self._store(domain_of(url), result)

    def _store(self, domain: str, result: ClearanceResult) -> CachedClearance:
        expires_in = max(1, min(self.settings.ttl_seconds, result.expires_in))
        return self.cache.put(domain, result.clearance, result.user_agent, ttl=expires_in)

    async def _replay_once(
        self, url: str, cached: CachedClearance, domain: str
    ) -> ReplayResult:
        call = functools.partial(
            replay,
            url,
            cached.clearance,
            cached.user_agent,
            domain,
            timeout=self.settings.replay_timeout,
        )
        try:
            return await anyio.to_thread.run_sync(call)
        except ValueError as exc:
            raise ApiError(BAD_REQUEST, str(exc)) from exc
        except Exception as exc:
            self.stats.record_error()
            raise ApiError(UPSTREAM_403, f"replay request failed: {exc}") from exc


def validate_url(raw: str) -> str:
    url = raw.strip()
    if not url or len(url) > 2048:
        raise ApiError(BAD_REQUEST, "url must be 1-2048 characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ApiError(BAD_REQUEST, "url must be an absolute http(s) URL")
    return url


async def require_token(request: Request) -> str:
    service: Service = request.app.state.service
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(
        token, service.settings.auth_token
    ):
        raise ApiError(AUTH_MISSING, "missing or invalid bearer token")
    return token


Auth = Annotated[str, Depends(require_token)]


def create_app(
    settings: Settings | None = None,
    backend: Backend | None = None,
    cache: SQLiteCache | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if not settings.auth_token:
        raise RuntimeError("auth token is empty; use load_settings() or pass one explicitly")
    backend = backend or CamoufoxBackend(
        headless=settings.headless,
        challenge_timeout=settings.challenge_timeout,
        poll_interval=settings.poll_interval,
    )
    cache = cache or SQLiteCache(settings.cache_path, settings.ttl_seconds)
    service = Service(settings, cache, backend)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        cache.close()

    app = FastAPI(title="cf-slayer", version=__version__, lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.payload(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return JSONResponse(
            ApiError(BAD_REQUEST, f"invalid request: {detail}").payload(), status_code=400
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            ApiError(SOLVE_FAILED, "internal error", status_code=500).payload(),
            status_code=500,
        )

    @app.post("/solve")
    async def solve(payload: SolveRequest, _: Auth) -> dict[str, Any]:
        if payload.want != "clearance":
            raise ApiError(BAD_REQUEST, "v1 supports only want='clearance'")
        return await service.solve(validate_url(payload.url))

    @app.get("/replay")
    async def replay_target(url: Annotated[str, Query()], _: Auth) -> dict[str, Any]:
        return await service.replay(validate_url(url))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"state": "ready", "browser": service.backend.name}

    @app.get("/stats")
    async def stats(_: Auth) -> dict[str, int | float]:
        snapshot = service.stats.snapshot()
        snapshot["cached_domains"] = service.cache.count()
        return snapshot

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazily build the ASGI app so importing this module has no side effects."""
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
