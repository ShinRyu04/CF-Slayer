from __future__ import annotations

import time
from typing import Self

import pytest

from solver.engine import Camoufox后端, 后端, 提取域名
from solver.errors import 求解失败, 求解超时

用户代理文本 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
)


class 假页面:
    def __init__(self, 上下文: 假上下文) -> None:
        self._上下文 = 上下文

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self._上下文.跳转调用.append((url, wait_until, timeout))
        if self._上下文.跳转时抛错 is not None:
            raise self._上下文.跳转时抛错

    def wait_for_timeout(self, milliseconds: int) -> None:
        self._上下文.轮询次数 += 1

    def evaluate(self, script: str) -> str:
        if "document.cookie" in script:
            return ""
        if "userAgent" in script:
            return self._上下文.用户代理
        raise AssertionError(f"未预期的脚本：{script}")


class 假上下文:
    def __init__(
        self,
        *,
        出饼干前轮询次数: int,
        用户代理: str = 用户代理文本,
        剩余秒数: float = 1200.0,
        跳转时抛错: Exception | None = None,
    ) -> None:
        self.出饼干前轮询次数 = 出饼干前轮询次数
        self.用户代理 = 用户代理
        self.剩余秒数 = 剩余秒数
        self.跳转时抛错 = 跳转时抛错
        self.轮询次数 = 0
        self.已关闭 = False
        self.跳转调用: list[tuple[str, str | None, int | None]] = []

    def new_page(self) -> 假页面:
        return 假页面(self)

    def cookies(self) -> list[dict[str, object]]:
        if self.跳转时抛错 is not None:
            return []
        if self.轮询次数 < self.出饼干前轮询次数:
            return []
        return [
            {
                "name": "cf_clearance",
                "value": "fake-clearance",
                "expires": time.time() + self.剩余秒数,
            }
        ]

    def close(self) -> None:
        self.已关闭 = True


class 假浏览器:
    def __init__(self, 上下文: 假上下文) -> None:
        self.上下文 = 上下文
        self.已关闭 = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *异常信息: object) -> None:
        self.已关闭 = True

    def new_context(self) -> 假上下文:
        return self.上下文


def 造后端(
    上下文: 假上下文, *, 挑战超时: float = 5.0, 轮询间隔: float = 0.001
) -> tuple[Camoufox后端, 假浏览器]:
    浏览器 = 假浏览器(上下文)
    后端对象 = Camoufox后端(
        挑战超时=挑战超时,
        轮询间隔=轮询间隔,
        工厂=lambda: 浏览器,
    )
    return 后端对象, 浏览器


def test_求解收集饼干与用户代理():
    上下文 = 假上下文(出饼干前轮询次数=2)
    后端对象, 浏览器 = 造后端(上下文)

    结果 = 后端对象.求解("https://target.example/path?q=1", 超时=5.0)

    assert 结果.凭据 == "fake-clearance"
    assert 结果.用户代理 == 用户代理文本
    assert 结果.域名 == "target.example"
    assert 1190 <= 结果.剩余秒数 <= 1200
    assert 结果.耗时秒 >= 0.0
    assert 上下文.轮询次数 == 2
    assert 上下文.已关闭 is True
    assert 浏览器.已关闭 is True
    assert 上下文.跳转调用[0][0] == "https://target.example/path?q=1"
    assert 上下文.跳转调用[0][1] == "domcontentloaded"


def test_无饼干时求解超时():
    上下文 = 假上下文(出饼干前轮询次数=10**9)
    后端对象, 浏览器 = 造后端(上下文, 挑战超时=0.05, 轮询间隔=0.005)

    with pytest.raises(求解超时):
        后端对象.求解("https://target.example", 超时=5.0)

    assert 上下文.已关闭 is True
    assert 浏览器.已关闭 is True


def test_跳转失败报告为求解失败():
    上下文 = 假上下文(
        出饼干前轮询次数=0, 跳转时抛错=RuntimeError("net::ERR_FAILED")
    )
    后端对象, _ = 造后端(上下文)

    with pytest.raises(求解失败, match="net::ERR_FAILED"):
        后端对象.求解("https://target.example", 超时=5.0)

    assert 上下文.已关闭 is True


def test_浏览器启动失败报告为求解失败():
    def 爆炸工厂():
        raise RuntimeError("firefox binary missing")

    后端对象 = Camoufox后端(工厂=爆炸工厂)
    with pytest.raises(求解失败, match="firefox binary missing"):
        后端对象.求解("https://target.example", 超时=5.0)


def test_缺失用户代理时拒绝发凭据():
    上下文 = 假上下文(出饼干前轮询次数=0, 用户代理="")
    后端对象, _ = 造后端(上下文, 挑战超时=0.02, 轮询间隔=0.005)

    with pytest.raises(求解超时):
        后端对象.求解("https://target.example", 超时=5.0)


def test_提取域名规范化主机():
    assert 提取域名("https://Target.Example:8443/a") == "target.example"
    assert 提取域名("http://sub.target.example/") == "sub.target.example"
    assert 提取域名("not a url") == ""


def test_后端协议被满足():
    后端对象, _ = 造后端(假上下文(出饼干前轮询次数=0))
    assert isinstance(后端对象, 后端)
    assert 后端对象.名称 == "camoufox"
