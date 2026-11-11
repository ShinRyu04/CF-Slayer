from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from solver.chain import 后端链, 链令牌超时
from solver.config import 配置
from solver.engine import Camoufox后端, 令牌结果, 提取令牌
from solver.errors import 令牌不受支持, 求解失败, 求解超时
from solver.main import 创建应用
from tests.conftest import 鉴权头

目标 = "https://target.example/login"
站点密钥 = "0x" + "a" * 24
用户代理文本 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
)


# ---------- 纯函数：从页面 HTML 提取令牌 ----------


def test_提取令牌_属性顺序与引号风格():
    assert 提取令牌(
        '<input type="hidden" name="cf-turnstile-response" value="token-a">'
    ) == "token-a"
    assert 提取令牌(
        "<input value='token-b' NAME='cf-turnstile-response'>"
    ) == "token-b"
    assert 提取令牌(
        '<div><input value=裸值 name=CF-TURNSTILE-RESPONSE></div>'
    ) == "裸值"


def test_提取令牌_缺失或为空时返回None():
    assert 提取令牌("<html><body>没有组件</body></html>") is None
    assert 提取令牌('<input name="cf-turnstile-response" value="">') is None
    assert 提取令牌('<input name="other" value="x">') is None


# ---------- 链路：能力探测与跳过 ----------


class 仅放行后端:
    名称 = "pydoll"

    def __init__(self) -> None:
        self.调用次数 = 0

    def 求解(self, 网址: str, 超时: float = 120.0):
        self.调用次数 += 1
        raise AssertionError("不该走放行路径")


class 令牌桩后端:
    名称 = "camoufox"

    def __init__(self, *, 错误: Exception | None = None) -> None:
        self.令牌 = "0." + "t" * 2000
        self.用户代理 = 用户代理文本
        self.耗时秒 = 7.5
        self.错误 = 错误
        self.调用: list[tuple[str, str, float]] = []

    def 求解(self, 网址: str, 超时: float = 120.0):
        raise AssertionError("不该走放行路径")

    def 求解令牌(
        self, 网址: str, sitekey: str, 超时: float = 60.0
    ) -> 令牌结果:
        self.调用.append((网址, sitekey, 超时))
        if self.错误 is not None:
            raise self.错误
        return 令牌结果(令牌=self.令牌, 用户代理=self.用户代理, 耗时秒=self.耗时秒)


def test_令牌链跳过不支持的后端():
    不支持的 = 仅放行后端()
    支持者 = 令牌桩后端()
    链路 = 后端链([不支持的, 支持者])

    结果 = 链路.求解令牌(目标, 站点密钥, 超时=30.0)

    assert 结果.令牌 == 支持者.令牌
    assert 不支持的.调用次数 == 0
    assert 支持者.调用 == [(目标, 站点密钥, 30.0)]


def test_令牌链失败时同时记录跳过与失败():
    失败者 = 令牌桩后端(错误=求解失败("widget 崩溃"))
    链路 = 后端链([仅放行后端(), 失败者])

    with pytest.raises(求解失败) as 信息:
        链路.求解令牌(目标, 站点密钥, 超时=30.0)

    尝试 = 信息.value.尝试记录
    assert [条目.后端名 for 条目 in 尝试] == ["pydoll", "camoufox"]
    assert "不支持 Turnstile" in str(尝试[0].错误)
    assert 尝试[1].错误 == "widget 崩溃"


def test_全部后端不支持时抛出令牌不受支持():
    链路 = 后端链([仅放行后端(), 仅放行后端()])

    with pytest.raises(令牌不受支持) as 信息:
        链路.求解令牌(目标, 站点密钥, 超时=5.0)

    异常 = 信息.value
    assert isinstance(异常, 求解失败)
    assert [条目.后端名 for 条目 in 异常.尝试记录] == ["pydoll", "pydoll"]
    assert all(条目.成功 is False for 条目 in 异常.尝试记录)
    assert "不支持 Turnstile" in str(异常)
    assert "没有后端支持 Turnstile" in str(异常)


def test_支持令牌的后端全部超时():
    首个 = 令牌桩后端(错误=求解超时("令牌一"))
    次个 = 令牌桩后端(错误=求解超时("令牌二"))
    链路 = 后端链([首个, 次个])

    with pytest.raises(链令牌超时) as 信息:
        链路.求解令牌(目标, 站点密钥, 超时=1.0)

    assert isinstance(信息.value, 求解超时)
    assert [条目.后端名 for 条目 in 信息.value.尝试记录] == ["camoufox", "camoufox"]


# ---------- 服务：路由、不碰缓存、统计 ----------


class 假缓存:
    """任何读写都被视为违约；令牌路径不该碰它。"""

    def __init__(self) -> None:
        self.操作: list[str] = []

    def 查询(self, *参数, **关键字):
        self.操作.append("查询")
        raise AssertionError("令牌路径不得查询缓存")

    def 写入(self, *参数, **关键字):
        self.操作.append("写入")
        raise AssertionError("令牌路径不得写入缓存")

    def 删除(self, *参数, **关键字):
        self.操作.append("删除")
        raise AssertionError("令牌路径不得删除缓存")

    def 计数(self) -> int:
        self.操作.append("计数")
        return 0

    def 关闭(self) -> None:
        pass


def _配置(tmp_path) -> 配置:
    return 配置(
        _env_file=None,
        auth_token="test-token",
        cache_path=str(tmp_path / "cache.sqlite3"),
        ttl_seconds=1800,
        solve_timeout=5.0,
        challenge_timeout=2.0,
        poll_interval=0.01,
        replay_timeout=5.0,
        headless=True,
    )


def _应用(tmp_path):
    后端对象 = 令牌桩后端()
    缓存 = 假缓存()
    应用 = 创建应用(
        配置对象=_配置(tmp_path), 后端对象=后端对象, 缓存=缓存
    )
    return TestClient(应用), 后端对象, 缓存


def test_缺少sitekey返回400(tmp_path):
    客户端, 后端对象, 缓存 = _应用(tmp_path)
    with 客户端:
        响应 = 客户端.post(
            "/solve", json={"url": 目标, "want": "turnstile"}, headers=鉴权头
        )
    assert 响应.status_code == 400
    assert 响应.json()["error"]["code"] == "BAD_REQUEST"
    assert 后端对象.调用 == []
    assert 缓存.操作 == []


def test_非法sitekey返回400(tmp_path):
    客户端, 后端对象, _ = _应用(tmp_path)
    with 客户端:
        响应 = 客户端.post(
            "/solve",
            json={"url": 目标, "want": "turnstile", "sitekey": "0x短"},
            headers=鉴权头,
        )
    assert 响应.status_code == 400
    assert "sitekey" in 响应.json()["error"]["message"]
    assert 后端对象.调用 == []


def test_未知want返回400(客户端):
    响应 = 客户端.post(
        "/solve", json={"url": 目标, "want": "clearance-v2"}, headers=鉴权头
    )
    assert 响应.status_code == 400
    assert "仅支持 clearance 或 turnstile" in 响应.json()["error"]["message"]


def test_求解令牌响应契约且不碰缓存(tmp_path):
    客户端, 后端对象, 缓存 = _应用(tmp_path)
    with 客户端:
        首次 = 客户端.post(
            "/solve",
            json={"url": 目标, "want": "turnstile", "sitekey": 站点密钥},
            headers=鉴权头,
        )
        第二次 = 客户端.post(
            "/solve",
            json={"url": 目标, "want": "turnstile", "sitekey": 站点密钥},
            headers=鉴权头,
        )
        统计 = 客户端.get("/stats", headers=鉴权头).json()

    assert 首次.status_code == 200, 首次.text
    assert set(首次.json()) == {
        "want",
        "token",
        "user_agent",
        "domain",
        "solve_seconds",
        "cached",
    }
    正文 = 首次.json()
    assert 正文["want"] == "turnstile"
    assert 正文["token"] == 后端对象.令牌
    assert 正文["user_agent"] == 用户代理文本
    assert 正文["domain"] == "target.example"
    assert 正文["solve_seconds"] == 7.5
    assert 正文["cached"] is False

    # 令牌一次性：两次请求必须各解一次，且不查/不写缓存。
    assert 第二次.status_code == 200
    assert len(后端对象.调用) == 2
    assert 后端对象.调用[0] == (目标, 站点密钥, 5.0)
    assert 缓存.操作 == ["计数"]  # 只有 /stats 的计数，令牌路径零缓存访问
    assert 统计["tokens_solved"] == 2
    assert 统计["token_errors"] == 0


def test_令牌失败映射与统计(tmp_path):
    客户端, 后端对象, _ = _应用(tmp_path)
    后端对象.错误 = 求解失败("widget 渲染失败")
    with 客户端:
        响应 = 客户端.post(
            "/solve",
            json={"url": 目标, "want": "turnstile", "sitekey": 站点密钥},
            headers=鉴权头,
        )
        统计 = 客户端.get("/stats", headers=鉴权头).json()
    assert 响应.status_code == 502
    assert 响应.json()["error"]["code"] == "SOLVE_FAILED"
    assert 统计["tokens_solved"] == 0
    assert 统计["token_errors"] == 1


# ---------- Camoufox 后端：轮询与关闭 ----------


class 令牌假页面:
    def __init__(self, 上下文: 令牌假上下文) -> None:
        self._上下文 = 上下文

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self._上下文.跳转.append((url, wait_until, timeout))

    def evaluate(self, script: str) -> str:
        if "api.js" in script:
            return "ready"
        if "turnstile.render" in script:
            return "rendered"
        if "__cfSlayerToken" in script:
            return ""
        if "userAgent" in script:
            return 用户代理文本
        raise AssertionError(f"未预期的脚本：{script}")

    def content(self) -> str:
        序号 = min(self._上下文.轮询次数, len(self._上下文.正文序列) - 1)
        return self._上下文.正文序列[序号]

    def wait_for_timeout(self, milliseconds: int) -> None:
        self._上下文.轮询次数 += 1


class 令牌假上下文:
    def __init__(self, 正文序列: list[str], 用户代理: str = 用户代理文本) -> None:
        self.正文序列 = 正文序列
        self.用户代理 = 用户代理
        self.轮询次数 = 0
        self.已关闭 = False
        self.跳转: list[tuple[str, str | None, int | None]] = []

    def new_page(self) -> 令牌假页面:
        return 令牌假页面(self)

    def close(self) -> None:
        self.已关闭 = True


class 令牌假浏览器:
    def __init__(self, 上下文: 令牌假上下文) -> None:
        self.上下文 = 上下文
        self.已关闭 = False

    def __enter__(self):
        return self

    def __exit__(self, *异常信息: object) -> None:
        self.已关闭 = True

    def new_context(self) -> 令牌假上下文:
        return self.上下文


def _造令牌后端(上下文: 令牌假上下文, *, 令牌超时: float = 5.0):
    浏览器 = 令牌假浏览器(上下文)
    后端对象 = Camoufox后端(
        令牌超时=令牌超时,
        令牌轮询间隔=0.001,
        工厂=lambda: 浏览器,
    )
    return 后端对象, 浏览器


def test_Camoufox求解令牌先空白页再目标页并轮询():
    令牌正文 = '<input type="hidden" name="cf-turnstile-response" value="tok-123">'
    上下文 = 令牌假上下文(["<html>等待</html>", 令牌正文])
    后端对象, 浏览器 = _造令牌后端(上下文)

    结果 = 后端对象.求解令牌(目标, 站点密钥, 超时=5.0)

    assert 结果.令牌 == "tok-123"
    assert 结果.用户代理 == 用户代理文本
    assert 结果.耗时秒 >= 0.0
    assert [项[0] for 项 in 上下文.跳转] == ["about:blank", 目标]
    assert 上下文.轮询次数 == 1
    assert 上下文.已关闭 is True
    assert 浏览器.已关闭 is True


def test_Camoufox令牌未出现时超时并关闭():
    上下文 = 令牌假上下文(["<html>永远没有令牌</html>"])
    后端对象, 浏览器 = _造令牌后端(上下文, 令牌超时=0.02)
    with pytest.raises(求解超时, match="Turnstile 令牌"):
        后端对象.求解令牌(目标, 站点密钥, 超时=5.0)
    assert 上下文.已关闭 is True
    assert 浏览器.已关闭 is True


def test_Camoufox拒绝非法sitekey且不启动浏览器():
    启动次数 = 0

    def 工厂():
        nonlocal 启动次数
        启动次数 += 1
        raise AssertionError("不该启动浏览器")

    后端对象 = Camoufox后端(工厂=工厂)
    with pytest.raises(求解失败, match="sitekey"):
        后端对象.求解令牌(目标, "bad", 超时=5.0)
    assert 启动次数 == 0
