from __future__ import annotations

import asyncio
import threading

import pytest

from solver.cache import SQLite缓存
from solver.config import 配置
from solver.engine import 放行结果
from solver.errors import 求解失败
from solver.main import 服务
from solver.prewarm import 规范化目标, 预热器

目标网址 = "https://target.example/"
目标域名 = "target.example"


class 桩后端:
    def __init__(self, 错误: Exception | None = None) -> None:
        self.名称 = "stub"
        self.调用次数 = 0
        self._错误 = 错误

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        self.调用次数 += 1
        if self._错误 is not None:
            raise self._错误
        return 放行结果(
            凭据="value",
            用户代理="Mozilla/5.0 test",
            域名=目标域名,
            剩余秒数=1700,
            耗时秒=0.5,
        )


class 阻塞后端:
    名称 = "blocking"

    def __init__(self) -> None:
        self.调用次数 = 0
        self.已开始 = threading.Event()
        self.放行 = threading.Event()

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        self.调用次数 += 1
        self.已开始.set()
        self.放行.wait(timeout=10)
        return 放行结果(
            凭据="value",
            用户代理="Mozilla/5.0 test",
            域名=目标域名,
            剩余秒数=1700,
            耗时秒=0.5,
        )


def _造(tmp_path, 后端对象, *, 余量: int = 300, 当前时刻=lambda: 1000.0):
    配置对象 = 配置(
        _env_file=None,
        auth_token="prewarm-token",
        cache_path=str(tmp_path / "cache.sqlite3"),
        ttl_seconds=1800,
    )
    缓存 = SQLite缓存(配置对象.cache_path, 配置对象.ttl_seconds)
    服务实例 = 服务(配置对象, 缓存, 后端对象)
    预热器实例 = 预热器(
        服务实例, [目标网址], 余量秒=余量, 间隔=0.01, 当前时刻=当前时刻
    )
    return 服务实例, 缓存, 预热器实例


@pytest.mark.parametrize(
    ("原始值", "期望值"),
    [
        ("steamdb.info", "https://steamdb.info/"),
        ("steamdb.info/", "https://steamdb.info/"),
        ("https://steamdb.info", "https://steamdb.info"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/"),
        ("  example.com  ", "https://example.com/"),
    ],
)
def test_规范化目标(原始值, 期望值):
    assert 规范化目标(原始值) == 期望值


def test_新鲜记录被跳过(tmp_path):
    后端对象 = 桩后端()
    服务实例, 缓存, 预热器实例 = _造(tmp_path, 后端对象)
    缓存.写入(目标域名, "old", "Mozilla/5.0", 有效期=1800, 当前时刻=1000.0)

    asyncio.run(预热器实例.轮询一轮())

    assert 后端对象.调用次数 == 0
    assert 服务实例.统计.快照()["solves"] == 0
    缓存.关闭()


def test_余量内的记录被续期(tmp_path):
    后端对象 = 桩后端()
    服务实例, 缓存, 预热器实例 = _造(
        tmp_path, 后端对象, 当前时刻=lambda: 1000.0 + 1600
    )
    缓存.写入(目标域名, "old", "Mozilla/5.0", 有效期=1800, 当前时刻=1000.0)

    asyncio.run(预热器实例.轮询一轮())

    assert 后端对象.调用次数 == 1
    assert 服务实例.统计.快照()["solves"] == 1
    更新后 = 缓存.查询(目标域名, 当前时刻=1000.0 + 1601)
    assert 更新后 is not None and 更新后.凭据 == "value"
    缓存.关闭()


def test_缺失记录被求解(tmp_path):
    后端对象 = 桩后端()
    _, 缓存, 预热器实例 = _造(tmp_path, 后端对象)

    asyncio.run(预热器实例.轮询一轮())

    assert 后端对象.调用次数 == 1
    assert 缓存.查询(目标域名) is not None
    缓存.关闭()


def test_错误被计数且不抛出(tmp_path):
    后端对象 = 桩后端(错误=求解失败("被拦截"))
    服务实例, 缓存, 预热器实例 = _造(tmp_path, 后端对象)

    asyncio.run(预热器实例.轮询一轮())

    统计 = 服务实例.统计.快照()
    assert 统计["prewarm_errors"] == 1
    assert 统计["solves"] == 0
    assert 缓存.查询(目标域名) is None
    缓存.关闭()


def test_并发轮询不会重复求解(tmp_path):
    后端对象 = 阻塞后端()
    _, 缓存, 预热器实例 = _造(tmp_path, 后端对象)

    async def 场景() -> None:
        首个 = asyncio.create_task(预热器实例.轮询一轮())
        while not 后端对象.已开始.is_set():
            await asyncio.sleep(0.005)
        次个 = asyncio.create_task(预热器实例.轮询一轮())
        await asyncio.sleep(0.02)
        assert 后端对象.调用次数 == 1
        后端对象.放行.set()
        await asyncio.gather(首个, 次个)

    asyncio.run(场景())
    assert 后端对象.调用次数 == 1
    缓存.关闭()


def test_停止信号后运行退出(tmp_path):
    后端对象 = 桩后端()
    _, 缓存, 预热器实例 = _造(tmp_path, 后端对象)

    async def 场景() -> None:
        停止信号 = asyncio.Event()
        任务 = asyncio.create_task(预热器实例.运行(停止信号))
        while 后端对象.调用次数 == 0:
            await asyncio.sleep(0.005)
        停止信号.set()
        await asyncio.wait_for(任务, timeout=2)

    asyncio.run(场景())
    缓存.关闭()
