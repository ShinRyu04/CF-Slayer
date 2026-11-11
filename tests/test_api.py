from __future__ import annotations

import solver.main as 主模块
from client.replay import 回放结果
from solver.engine import 提取域名, 放行结果
from solver.errors import 求解失败, 求解超时
from tests.conftest import 鉴权头

目标 = "https://target.example/page"


def test_健康检查公开(客户端):
    响应 = 客户端.get("/health")
    assert 响应.status_code == 200
    assert 响应.json() == {"state": "ready", "browser": "camoufox"}


def test_求解需要令牌(客户端):
    响应 = 客户端.post("/solve", json={"url": 目标})
    assert 响应.status_code == 401
    assert 响应.json()["error"]["code"] == "AUTH_MISSING"


def test_求解拒绝错误令牌(客户端):
    响应 = 客户端.post(
        "/solve", json={"url": 目标}, headers={"Authorization": "Bearer nope"}
    )
    assert 响应.status_code == 401
    assert 响应.json()["error"]["code"] == "AUTH_MISSING"


def test_求解拒绝非HTTP网址(客户端):
    响应 = 客户端.post("/solve", json={"url": "ftp://target.example"}, headers=鉴权头)
    assert 响应.status_code == 400
    assert 响应.json()["error"]["code"] == "BAD_REQUEST"


def test_求解拒绝缺失网址(客户端):
    响应 = 客户端.post("/solve", json={}, headers=鉴权头)
    assert 响应.status_code == 400
    assert 响应.json()["error"]["code"] == "BAD_REQUEST"


def test_求解拒绝未知want(客户端):
    响应 = 客户端.post(
        "/solve", json={"url": 目标, "want": "turnstile"}, headers=鉴权头
    )
    assert 响应.status_code == 400
    assert 响应.json()["error"]["code"] == "BAD_REQUEST"


def test_首次求解成功后命中缓存(客户端, 假后端):
    首次 = 客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    assert 首次.status_code == 200
    正文 = 首次.json()
    assert 正文["clearance"] == 假后端.凭据
    assert 正文["user_agent"] == 假后端.用户代理
    assert 正文["domain"] == "target.example"
    assert 正文["cached"] is False
    assert 正文["solve_seconds"] == 1.25
    assert 1 <= 正文["expires_in"] <= 1700

    第二次 = 客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    assert 第二次.status_code == 200
    缓存正文 = 第二次.json()
    assert 缓存正文["cached"] is True
    assert 缓存正文["clearance"] == 正文["clearance"]
    assert 缓存正文["solve_seconds"] == 0.0
    assert 假后端.调用次数 == 1


def test_求解超时映射504(客户端, 假后端):
    假后端.错误 = 求解超时("无饼干")
    响应 = 客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 504
    assert 响应.json()["error"]["code"] == "SOLVE_TIMEOUT"


def test_求解失败映射502(客户端, 假后端):
    假后端.错误 = 求解失败("挑战未通过")
    响应 = 客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 502
    assert 响应.json()["error"]["code"] == "SOLVE_FAILED"


def test_统计需要鉴权(客户端):
    assert 客户端.get("/stats").status_code == 401


def test_统计记录命中求解与错误(客户端, 假后端):
    客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    假后端.错误 = 求解失败("炸了")
    客户端.post("/solve", json={"url": "https://other.example"}, headers=鉴权头)

    统计 = 客户端.get("/stats", headers=鉴权头).json()
    assert 统计["solves"] == 1
    assert 统计["cache_hits"] == 1
    assert 统计["errors"] == 1
    assert 统计["avg_solve_seconds"] == 1.25
    assert 统计["cached_domains"] == 1


def _先求解(客户端) -> None:
    响应 = 客户端.post("/solve", json={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 200, 响应.text


def test_回放需要令牌(客户端):
    assert 客户端.get("/replay", params={"url": 目标}).status_code == 401


def test_回放使用缓存凭据(客户端, monkeypatch):
    _先求解(客户端)
    记录: dict[str, object] = {}

    def 假回放(url, clearance, user_agent, domain="", *, 超时=30.0):
        记录.update(
            url=url, clearance=clearance, user_agent=user_agent, domain=domain, 超时=超时
        )
        return 回放结果(
            状态码=200, 长度=4321, 挑战页=False, 耗时秒=0.4, 网址=url
        )

    monkeypatch.setattr(主模块, "回放", 假回放)

    响应 = 客户端.get("/replay", params={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 200
    正文 = 响应.json()
    assert 正文["status"] == 200
    assert 正文["length"] == 4321
    assert 正文["challenge_page"] is False
    assert 正文["resolved"] is False
    assert 记录["clearance"] == "clearance-token-value"
    assert 记录["domain"] == "target.example"
    assert "Firefox" in str(记录["user_agent"])


def test_回放遇403触发一次重解(客户端, 假后端, monkeypatch):
    调用列表: list[str] = []

    def 假回放(url, clearance, user_agent, domain="", *, 超时=30.0):
        调用列表.append(clearance)
        被拦截 = len(调用列表) == 1
        return 回放结果(
            状态码=403 if 被拦截 else 200,
            长度=512 if 被拦截 else 9999,
            挑战页=被拦截,
            耗时秒=0.1,
            网址=url,
        )

    monkeypatch.setattr(主模块, "回放", 假回放)

    响应 = 客户端.get("/replay", params={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 200
    正文 = 响应.json()
    assert 正文["status"] == 200
    assert 正文["length"] == 9999
    assert 正文["resolved"] is True
    assert 假后端.调用次数 == 2
    assert len(调用列表) == 2


def test_重解后仍被拦截映射UPSTREAM403(客户端, 假后端, monkeypatch):
    def 假回放(url, clearance, user_agent, domain="", *, 超时=30.0):
        return 回放结果(
            状态码=403, 长度=512, 挑战页=True, 耗时秒=0.1, 网址=url
        )

    monkeypatch.setattr(主模块, "回放", 假回放)

    响应 = 客户端.get("/replay", params={"url": 目标}, headers=鉴权头)
    assert 响应.status_code == 502
    assert 响应.json()["error"]["code"] == "UPSTREAM_403"
    assert 假后端.调用次数 == 2


def test_引擎结果域名已规范化(服务实例):
    结果 = 服务实例.后端.求解("https://UPPER.Example/", 超时=1.0)
    assert 结果.域名 == 提取域名("https://UPPER.Example/") == "upper.example"
    assert isinstance(结果, 放行结果)
