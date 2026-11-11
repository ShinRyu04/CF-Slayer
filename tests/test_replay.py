from __future__ import annotations

import pytest

from client.replay import is_challenge_page, replay

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"


class FakeResponse:
    def __init__(self, status_code: int, content: bytes, url: str) -> None:
        self.status_code = status_code
        self.content = content
        self.url = url


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []
        self.closed = False

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


def test_replay_sends_clearance_and_exact_user_agent():
    session = FakeSession(FakeResponse(200, b"<html>ok</html>", "https://target.example/"))

    result = replay(
        "https://target.example/",
        "clearance-value",
        UA,
        "target.example",
        session_factory=lambda: session,
    )

    assert result.status == 200
    assert result.length == len(b"<html>ok</html>")
    assert result.challenge_page is False
    sent = session.requests[0]
    assert sent["cookies"] == {"cf_clearance": "clearance-value"}
    assert sent["headers"]["User-Agent"] == UA  # type: ignore[index]
    assert session.closed is True


def test_replay_refuses_domain_mismatch():
    with pytest.raises(ValueError, match="cannot be replayed"):
        replay(
            "https://other.example/",
            "clearance-value",
            UA,
            "target.example",
            session_factory=lambda: FakeSession(FakeResponse(200, b"", "https://other.example/")),
        )


def test_replay_requires_user_agent():
    with pytest.raises(ValueError, match="user_agent"):
        replay("https://target.example/", "clearance-value", "")


def test_challenge_detection():
    assert is_challenge_page(403, b"") is True
    assert is_challenge_page(503, b"") is True
    assert is_challenge_page(200, b"<title>Just a moment...</title>") is True
    assert is_challenge_page(200, b"<html>real page</html>") is False
    assert is_challenge_page(200, b"") is False
