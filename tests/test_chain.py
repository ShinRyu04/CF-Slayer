from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from solver.chain import 后端链, 链求解失败, 链求解超时
from solver.config import 配置
from solver.engine import 单次尝试, 后端, 放行结果
from solver.errors import 求解失败, 求解超时
from solver.main import 创建应用

令牌 = "chain-test-token"
鉴权头 = {"Authorization": f"Bearer {令牌}"}
目标网址 = "https://target.example/"


def 造结果(凭据值: str = "value") -> 放行结果:
    return 放行结果(
        凭据=凭据值,
        用户代理="Mozilla/5.0 test",
        域名="target.example",
        剩余秒数=1700,
        耗时秒=1.0,
    )


class 桩后端:
    def __init__(
        self,
        名称: str,
        *,
        结果: 放行结果 | None = None,
        错误: Exception | None = None,
        可用状态: tuple[bool, str] | None = None,
        探测错误: Exception | None = None,
    ) -> None:
        self.名称 = 名称
        self._结果 = 结果 or 造结果()
        self._错误 = 错误
        self._可用状态 = 可用状态
        self._探测错误 = 探测错误
        self.调用次数 = 0
        self.超时参数: list[float] = []

    def 可用(self) -> tuple[bool, str]:
        if self._探测错误 is not None:
            raise self._探测错误
        return self._可用状态 if self._可用状态 is not None else (True, "")

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        self.调用次数 += 1
        self.超时参数.append(超时)
        if self._错误 is not None:
            raise self._错误
        return self._结果


def test_首次成功即短路():
    首个 = 桩后端("camoufox")
    次个 = 桩后端("pydoll")
    链路 = 后端链([首个, 次个])

    结果 = 链路.求解(目标网址, 超时=42.0)

    assert 结果.凭据 == 首个._结果.凭据
    assert [条目.后端名 for 条目 in 结果.尝试记录] == ["camoufox"]
    assert 结果.尝试记录[0].成功 is True
    assert 结果.尝试记录[0].错误 is None
    assert 首个.调用次数 == 1
    assert 次个.调用次数 == 0
    assert 首个.超时参数 == [42.0]


def test_顺序被遵守且失败后回退():
    首个 = 桩后端("camoufox", 错误=求解失败("被拦截"))
    次个 = 桩后端("pydoll", 结果=造结果("pydoll-value"))
    链路 = 后端链([首个, 次个])

    结果 = 链路.求解(目标网址, 超时=5.0)

    assert 结果.凭据 == "pydoll-value"
    assert [条目.后端名 for 条目 in 结果.尝试记录] == ["camoufox", "pydoll"]
    assert [条目.成功 for 条目 in 结果.尝试记录] == [False, True]
    assert 结果.尝试记录[0].错误 == "被拦截"
    assert 结果.尝试记录[1].错误 is None
    assert all(条目.秒数 >= 0 for 条目 in 结果.尝试记录)


def test_全部失败时汇总尝试记录():
    链路 = 后端链(
        [
            桩后端("camoufox", 错误=求解失败("没有饼干")),
            桩后端("pydoll", 错误=求解失败("Chrome 崩溃")),
        ]
    )

    with pytest.raises(链求解失败) as 信息:
        链路.求解(目标网址, 超时=5.0)

    异常 = 信息.value
    assert isinstance(异常, 求解失败)
    assert [条目.后端名 for 条目 in 异常.尝试记录] == ["camoufox", "pydoll"]
    assert all(条目.成功 is False for 条目 in 异常.尝试记录)
    assert "camoufox: 没有饼干" in str(异常)
    assert "pydoll: Chrome 崩溃" in str(异常)


def test_全部超时抛出超时并带尝试记录():
    链路 = 后端链(
        [
            桩后端("camoufox", 错误=求解超时("超时一")),
            桩后端("pydoll", 错误=求解超时("超时二")),
        ]
    )

    with pytest.raises(链求解超时) as 信息:
        链路.求解(目标网址, 超时=1.0)

    assert isinstance(信息.value, 求解超时)
    assert len(信息.value.尝试记录) == 2


def test_混合超时与失败不算超时():
    链路 = 后端链(
        [
            桩后端("camoufox", 错误=求解超时("超时一")),
            桩后端("pydoll", 错误=求解失败("炸了")),
        ]
    )

    with pytest.raises(链求解失败):
        链路.求解(目标网址, 超时=1.0)


def test_不可用后端被跳过且不调用求解():
    不可用 = 桩后端("pydoll", 可用状态=(False, "未找到 Chrome/Chromium"))
    回退 = 桩后端("camoufox")
    链路 = 后端链([不可用, 回退])

    结果 = 链路.求解(目标网址, 超时=5.0)

    assert 不可用.调用次数 == 0
    assert 回退.调用次数 == 1
    assert 结果.尝试记录[0].后端名 == "pydoll"
    assert 结果.尝试记录[0].成功 is False
    assert "未找到 Chrome/Chromium" in str(结果.尝试记录[0].错误)
    assert 结果.尝试记录[0].秒数 == 0.0


def test_可用性探测异常被隔离():
    损坏 = 桩后端("pydoll", 探测错误=RuntimeError("探测爆炸"))
    回退 = 桩后端("camoufox")
    链路 = 后端链([损坏, 回退])

    结果 = 链路.求解(目标网址, 超时=5.0)

    assert 结果.尝试记录[0].成功 is False
    assert "探测爆炸" in str(结果.尝试记录[0].错误)
    assert 损坏.调用次数 == 0


def test_后端未预期异常时回退():
    崩溃 = 桩后端("camoufox", 错误=RuntimeError("段错误"))
    回退 = 桩后端("pydoll")
    链路 = 后端链([崩溃, 回退])

    结果 = 链路.求解(目标网址, 超时=5.0)

    assert 结果.尝试记录[0].错误 == "RuntimeError: 段错误"
    assert 回退.调用次数 == 1


def test_链路满足后端协议并使用首个名称():
    链路 = 后端链([桩后端("camoufox"), 桩后端("pydoll")])
    assert isinstance(链路, 后端)
    assert 链路.名称 == "camoufox"


def test_空链路被拒绝():
    with pytest.raises(ValueError, match="至少需要一个后端"):
        后端链([])


def test_尝试字典结构():
    条目 = 单次尝试(后端名="pydoll", 成功=False, 错误="不行", 秒数=1.23456)
    assert 条目.转字典() == {
        "backend": "pydoll",
        "ok": False,
        "error": "不行",
        "seconds": 1.235,
    }


def _配置(tmp_path) -> 配置:
    return 配置(
        _env_file=None,
        auth_token=令牌,
        cache_path=str(tmp_path / "cache.sqlite3"),
        solve_timeout=5.0,
        backends="camoufox,pydoll",
        prewarm_domains="",
    )


def test_求解响应包含尝试记录(tmp_path):
    链路 = 后端链([桩后端("camoufox")])
    应用 = 创建应用(配置对象=_配置(tmp_path), 后端对象=链路)
    with TestClient(应用) as 客户端:
        响应 = 客户端.post("/solve", json={"url": 目标网址}, headers=鉴权头)
    assert 响应.status_code == 200
    正文 = 响应.json()
    assert 正文["attempts"] == [
        {"backend": "camoufox", "ok": True, "error": None, "seconds": 0.0}
    ]


def test_缓存命中返回空尝试记录(tmp_path):
    链路 = 后端链([桩后端("camoufox")])
    应用 = 创建应用(配置对象=_配置(tmp_path), 后端对象=链路)
    with TestClient(应用) as 客户端:
        客户端.post("/solve", json={"url": 目标网址}, headers=鉴权头)
        第二次 = 客户端.post("/solve", json={"url": 目标网址}, headers=鉴权头)
    assert 第二次.json()["cached"] is True
    assert 第二次.json()["attempts"] == []


def test_仅pydoll且无Chrome时返回清晰错误(tmp_path):
    配置对象 = _配置(tmp_path)
    配置对象.backends = "pydoll"
    配置对象.chrome_path = str(tmp_path / "no-such-chrome.exe")
    应用 = 创建应用(配置对象=配置对象)  # 启动时不得崩溃

    with TestClient(应用, raise_server_exceptions=False) as 客户端:
        响应 = 客户端.post("/solve", json={"url": 目标网址}, headers=鉴权头)

    assert 响应.status_code == 502
    错误 = 响应.json()["error"]
    assert 错误["code"] == "SOLVE_FAILED"
    assert 错误["attempts"][0]["backend"] == "pydoll"
    assert "Chrome" in 错误["message"]
    assert "CF_SLAYER_CHROME_PATH" in 错误["attempts"][0]["error"]
