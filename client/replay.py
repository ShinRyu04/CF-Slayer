"""Replay a clearance with the exact UA the browser used.

Hard rules from the research: same egress IP, same UA, ~30 min TTL, per-domain
isolation. This module enforces the UA part; IP/TTL/domain are enforced by the
caller (cache key + expiry, same host).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: Substrings that identify a Cloudflare interstitial instead of real content.
CHALLENGE_MARKERS = (
    b"just a moment",
    b"cf-chl",
    b"challenge-platform",
    b"attention required",
    b"enable javascript and cookies to continue",
)

#: Only the first chunk needs scanning; challenge pages are small and JS bundles
#: in real pages can contain the markers in unrelated text.
_SCAN_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class ReplayResult:
    status: int
    length: int
    challenge_page: bool
    elapsed_seconds: float
    url: str


def is_challenge_page(status: int, body: bytes) -> bool:
    """Status-only detection misses CF's 200-with-interstitial variants."""
    if status in (403, 503):
        return True
    head = body[:_SCAN_BYTES].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def replay(
    url: str,
    clearance: str,
    user_agent: str,
    domain: str = "",
    *,
    timeout: float = 30.0,
    session_factory: Any | None = None,
) -> ReplayResult:
    """GET ``url`` with ``cf_clearance`` + the solve-time UA.

    The cookie is sent as a host-only cookie for the requested URL, which is the
    narrowest scope that still works for target domains and their subdomains.
    """
    if not clearance:
        raise ValueError("clearance must not be empty")
    if not user_agent:
        raise ValueError("user_agent must not be empty")
    if domain and domain_of(url) != domain.lower().rstrip("."):
        raise ValueError(f"clearance for {domain!r} cannot be replayed against {url!r}")

    start = time.monotonic()
    if session_factory is None:
        from curl_cffi import requests as cffi_requests

        def session_factory() -> Any:  # type: ignore[misc]
            return cffi_requests.Session(impersonate="firefox")

    with session_factory() as session:
        response = session.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies={"cf_clearance": clearance},
            timeout=timeout,
        )
        body: bytes = response.content or b""
        status = int(response.status_code)
        final_url = str(getattr(response, "url", url) or url)

    return ReplayResult(
        status=status,
        length=len(body),
        challenge_page=is_challenge_page(status, body),
        elapsed_seconds=round(time.monotonic() - start, 3),
        url=final_url,
    )


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.lower().rstrip(".")
