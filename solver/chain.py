"""Ordered backend fallback chain.

The chain implements the same ``Backend`` protocol as a single backend, so the
service does not care how many solvers stand behind it. Every try is recorded in
``ClearanceResult.attempts`` (or on the raised error) for observability.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import replace

from .engine import Attempt, Backend, ClearanceResult
from .errors import SolveFailed, SolveTimeout

log = logging.getLogger(__name__)


class ChainSolveFailed(SolveFailed):
    """All backends failed; ``attempts`` describes each try."""

    def __init__(self, message: str, attempts: Sequence[Attempt]) -> None:
        super().__init__(message)
        self.attempts = tuple(attempts)


class ChainSolveTimeout(SolveTimeout):
    """All backends timed out; ``attempts`` describes each try."""

    def __init__(self, message: str, attempts: Sequence[Attempt]) -> None:
        super().__init__(message)
        self.attempts = tuple(attempts)


class BackendChain:
    """Try backends in order, return the first clearance.

    A backend that declares itself unavailable (``available()`` returning
    ``(False, reason)``) is skipped without being called, so a machine without
    Chrome still runs the Camoufox path instead of crashing.
    """

    def __init__(self, backends: Sequence[Backend]) -> None:
        if not backends:
            raise ValueError("BackendChain needs at least one backend")
        self._backends = tuple(backends)
        self.name = self._backends[0].name

    @property
    def backends(self) -> tuple[Backend, ...]:
        return self._backends

    def solve(self, url: str, timeout: float = 120.0) -> ClearanceResult:
        attempts: list[Attempt] = []
        timeouts = 0

        for backend in self._backends:
            unavailable = _unavailable_reason(backend)
            if unavailable is not None:
                attempts.append(Attempt(backend.name, False, unavailable, 0.0))
                log.warning("backend %s skipped: %s", backend.name, unavailable)
                continue

            started = time.monotonic()
            try:
                result = backend.solve(url, timeout)
            except SolveTimeout as exc:
                timeouts += 1
                attempts.append(Attempt(backend.name, False, str(exc), time.monotonic() - started))
                log.warning("backend %s timed out in %.1fs", backend.name, attempts[-1].seconds)
                continue
            except SolveFailed as exc:
                attempts.append(Attempt(backend.name, False, str(exc), time.monotonic() - started))
                log.warning("backend %s failed: %s", backend.name, exc)
                continue
            except Exception as exc:  # one broken backend must not kill the chain
                attempts.append(
                    Attempt(
                        backend.name,
                        False,
                        f"{type(exc).__name__}: {exc}",
                        time.monotonic() - started,
                    )
                )
                log.exception("backend %s raised unexpectedly", backend.name)
                continue

            attempts.append(Attempt(backend.name, True, None, time.monotonic() - started))
            log.info("backend %s solved %s", backend.name, url)
            return replace(result, attempts=tuple(attempts))

        summary = "; ".join(f"{a.backend}: {a.error}" for a in attempts) or "no backends ran"
        message = f"all backends failed ({len(attempts)} attempt(s)): {summary}"
        if attempts and timeouts == len(attempts):
            raise ChainSolveTimeout(message, attempts)
        raise ChainSolveFailed(message, attempts)


def _unavailable_reason(backend: Backend) -> str | None:
    probe = getattr(backend, "available", None)
    if not callable(probe):
        return None
    try:
        ok, reason = probe()
    except Exception as exc:  # availability probing must never break dispatch
        return f"availability check failed: {type(exc).__name__}: {exc}"
    return None if ok else str(reason or "unavailable")
