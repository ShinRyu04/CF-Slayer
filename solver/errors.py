"""Error contract shared by the HTTP layer, engine and replay client."""

from __future__ import annotations

#: Error codes exposed in the JSON body: {"error": {"code": ..., "message": ...}}
SOLVE_TIMEOUT = "SOLVE_TIMEOUT"
SOLVE_FAILED = "SOLVE_FAILED"
UPSTREAM_403 = "UPSTREAM_403"
AUTH_MISSING = "AUTH_MISSING"
BAD_REQUEST = "BAD_REQUEST"

STATUS_BY_CODE = {
    SOLVE_TIMEOUT: 504,
    SOLVE_FAILED: 502,
    UPSTREAM_403: 502,
    AUTH_MISSING: 401,
    BAD_REQUEST: 400,
}


class ApiError(Exception):
    """An error that maps directly onto an HTTP response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code if status_code is not None else STATUS_BY_CODE.get(code, 500)
        self.extra = extra or {}

    def payload(self) -> dict[str, dict[str, object]]:
        body: dict[str, object] = {"code": self.code, "message": self.message}
        body.update(self.extra)
        return {"error": body}


class SolveError(Exception):
    """Backend failure. The HTTP layer translates it into an API error code."""


class SolveTimeout(SolveError):
    """The backend ran out of time before producing a clearance."""


class SolveFailed(SolveError):
    """The backend ran but did not produce a clearance."""
