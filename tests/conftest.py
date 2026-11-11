"""共享夹具。无网络、无真实浏览器。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from solver.config import 配置
from solver.engine import 提取域名, 放行结果
from solver.main import 创建应用

os.environ.setdefault("CF_SLAYER_AUTH_TOKEN", "test-token")

令牌 = "test-token"
鉴权头 = {"Authorization": f"Bearer {令牌}"}


class 假后端实现:
    """替代 Camoufox后端；记录调用，也可按需失败。"""

    名称 = "camoufox"

    def __init__(
        self,
        凭据: str = "clearance-token-value",
        用户代理: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) "
            "Gecko/20100101 Firefox/156.0"
        ),
        剩余秒数: int = 1700,
        耗时秒: float = 1.25,
    ) -> None:
        self.凭据 = 凭据
        self.用户代理 = 用户代理
        self.剩余秒数 = 剩余秒数
        self.耗时秒 = 耗时秒
        self.调用次数 = 0
        self.网址列表: list[str] = []
        self.错误: Exception | None = None

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        self.调用次数 += 1
        self.网址列表.append(网址)
        if self.错误 is not None:
            raise self.错误
        return 放行结果(
            凭据=self.凭据,
            用户代理=self.用户代理,
            域名=提取域名(网址),
            剩余秒数=self.剩余秒数,
            耗时秒=self.耗时秒,
        )


@pytest.fixture
def 配置对象(tmp_path) -> 配置:
    return 配置(
        _env_file=None,
        auth_token=令牌,
        cache_path=str(tmp_path / "cache.sqlite3"),
        ttl_seconds=1800,
        solve_timeout=5.0,
        challenge_timeout=2.0,
        poll_interval=0.01,
        replay_timeout=5.0,
        headless=True,
    )


@pytest.fixture
def 接口(配置对象: 配置):
    后端对象 = 假后端实现()
    应用 = 创建应用(配置对象=配置对象, 后端对象=后端对象)
    with TestClient(应用) as 客户端:
        yield 客户端, 后端对象, 应用.state.服务


@pytest.fixture
def 客户端(接口) -> TestClient:
    return 接口[0]


@pytest.fixture
def 假后端(接口) -> 假后端实现:
    return 接口[1]


@pytest.fixture
def 服务实例(接口):
    return 接口[2]
