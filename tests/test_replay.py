from __future__ import annotations

import pytest

from client.replay import 回放, 是挑战页

用户代理文本 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
)


class 假响应:
    def __init__(self, 状态码: int, 正文: bytes, 网址: str) -> None:
        self.status_code = 状态码
        self.content = 正文
        self.url = 网址


class 假会话:
    def __init__(self, 响应: 假响应) -> None:
        self.响应 = 响应
        self.请求列表: list[dict[str, object]] = []
        self.已关闭 = False

    def __enter__(self) -> 假会话:
        return self

    def __exit__(self, *异常信息: object) -> None:
        self.已关闭 = True

    def get(self, url: str, **kwargs: object) -> 假响应:
        self.请求列表.append({"url": url, **kwargs})
        return self.响应


def test_回放携带凭据与精确用户代理():
    会话 = 假会话(假响应(200, b"<html>ok</html>", "https://target.example/"))

    结果 = 回放(
        "https://target.example/",
        "clearance-value",
        用户代理文本,
        "target.example",
        会话工厂=lambda: 会话,
    )

    assert 结果.状态码 == 200
    assert 结果.长度 == len(b"<html>ok</html>")
    assert 结果.挑战页 is False
    发送 = 会话.请求列表[0]
    assert 发送["cookies"] == {"cf_clearance": "clearance-value"}
    assert 发送["headers"]["User-Agent"] == 用户代理文本  # type: ignore[index]
    assert 会话.已关闭 is True


def test_回放拒绝域名不匹配():
    with pytest.raises(ValueError, match="不能用于回放"):
        回放(
            "https://other.example/",
            "clearance-value",
            用户代理文本,
            "target.example",
            会话工厂=lambda: 假会话(
                假响应(200, b"", "https://other.example/")
            ),
        )


def test_回放要求用户代理():
    with pytest.raises(ValueError, match="用户代理"):
        回放("https://target.example/", "clearance-value", "")


def test_挑战页检测():
    assert 是挑战页(403, b"") is True
    assert 是挑战页(503, b"") is True
    assert 是挑战页(200, b"<title>Just a moment...</title>") is True
    assert 是挑战页(200, b"<html>real page</html>") is False
    assert 是挑战页(200, b"") is False
