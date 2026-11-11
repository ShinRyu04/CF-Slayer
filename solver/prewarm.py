"""Background prewarm: re-solve domains before their credentials expire.

Started by the app lifespan when ``CF_SLAYER_PREWARM_DOMAINS`` is non-empty.
Each tick checks the cache and only solves entries that are missing or inside
``prewarm_margin`` seconds of expiry. Per-domain in-flight tracking prevents
concurrent solves from overlapping ticks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .engine import domain_of
from .errors import ApiError

if TYPE_CHECKING:
    from .main import Service

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 60.0
DEFAULT_MARGIN = 300


def normalize_target(target: str) -> str:
    """``steamdb.info`` -> ``https://steamdb.info/``; full URLs pass through."""
    target = target.strip()
    if not target:
        return ""
    if "://" in target:
        return target
    host = target.rstrip("/")
    if urlsplit(host).netloc:
        return f"https://{host}/"
    return f"https://{host}/"


class PrewarmWorker:
    def __init__(
        self,
        service: Service,
        targets: Sequence[str],
        *,
        margin_seconds: int = DEFAULT_MARGIN,
        interval: float = DEFAULT_INTERVAL,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._service = service
        self._targets = tuple(
            url for url in (normalize_target(t) for t in targets) if domain_of(url)
        )
        self._margin = max(0, margin_seconds)
        self._interval = interval
        self._now = now
        self._in_flight: set[str] = set()

    @property
    def targets(self) -> tuple[str, ...]:
        return self._targets

    async def tick(self) -> None:
        for url in self._targets:
            domain = domain_of(url)
            if not domain or domain in self._in_flight:
                log.debug("prewarm skip %s (already solving)", domain or url)
                continue
            cached = self._service.cache.get(domain, now=self._now())
            if cached is not None and cached.remaining(self._now()) >= self._margin:
                continue

            self._in_flight.add(domain)
            try:
                result = await self._service.solve(url, force=True)
                log.info(
                    "prewarm solved %s in %ss (attempts=%d)",
                    domain,
                    result.get("solve_seconds"),
                    len(result.get("attempts") or ()),
                )
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._service.stats.record_prewarm_error()
                log.warning("prewarm failed for %s: %s (%s)", domain, exc.message, exc.code)
            except Exception:  # a prewarm tick must never kill the task
                self._service.stats.record_prewarm_error()
                log.exception("prewarm crashed for %s", domain)
            finally:
                self._in_flight.discard(domain)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        log.info(
            "prewarm started for %s (interval=%ss, margin=%ss)",
            ", ".join(self._targets),
            self._interval,
            self._margin,
        )
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the loop alive
                self._service.stats.record_prewarm_error()
                log.exception("prewarm tick failed")
            if stop is None:
                await asyncio.sleep(self._interval)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._interval)
        log.info("prewarm stopped")
