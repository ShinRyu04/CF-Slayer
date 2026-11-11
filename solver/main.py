"""FastAPI 服务：/solve、/replay、/health、/stats。"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

import anyio
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from client.replay import 回放, 回放结果

from . import __version__
from .cache import SQLite缓存, 缓存凭据
from .chain import 后端链
from .config import 加载配置, 配置
from .engine import Camoufox后端, 单次尝试, 后端, 提取域名, 放行结果
from .errors import (
    AUTH_MISSING,
    BAD_REQUEST,
    SOLVE_FAILED,
    SOLVE_TIMEOUT,
    UPSTREAM_403,
    接口错误,
    求解失败,
    求解超时,
)
from .prewarm import 预热器
from .pydoll_backend import Pydoll后端

日志 = logging.getLogger(__name__)


class 求解请求(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    want: str = "clearance"
    # 仅 want=turnstile 使用；clearance 路径忽略它。
    sitekey: str | None = None


@dataclass
class 统计:
    缓存命中: int = 0
    求解次数: int = 0
    错误次数: int = 0
    预热错误: int = 0
    令牌求解次数: int = 0
    令牌错误次数: int = 0
    总求解秒数: float = 0.0
    启动时刻: float = field(default_factory=time.time)
    _锁: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def 记录命中(self) -> None:
        with self._锁:
            self.缓存命中 += 1

    def 记录求解(self, 秒数: float) -> None:
        with self._锁:
            self.求解次数 += 1
            self.总求解秒数 += max(0.0, 秒数)

    def 记录错误(self) -> None:
        with self._锁:
            self.错误次数 += 1

    def 记录预热错误(self) -> None:
        with self._锁:
            self.预热错误 += 1

    def 记录令牌求解(self) -> None:
        with self._锁:
            self.令牌求解次数 += 1

    def 记录令牌错误(self) -> None:
        with self._锁:
            self.令牌错误次数 += 1

    def 快照(self) -> dict[str, int | float]:
        with self._锁:
            平均值 = self.总求解秒数 / self.求解次数 if self.求解次数 else 0.0
            return {
                "cache_hits": self.缓存命中,
                "solves": self.求解次数,
                "errors": self.错误次数,
                "prewarm_errors": self.预热错误,
                "avg_solve_seconds": round(平均值, 2),
                "tokens_solved": self.令牌求解次数,
                "token_errors": self.令牌错误次数,
                "uptime_seconds": round(time.time() - self.启动时刻, 1),
            }


class 服务:
    def __init__(self, 配置对象: 配置, 缓存: SQLite缓存, 后端对象: 后端) -> None:
        self.配置 = 配置对象
        self.缓存 = 缓存
        self.后端 = 后端对象
        self.统计 = 统计()

    async def 求解(self, 网址: str, 强制: bool = False) -> dict[str, Any]:
        域名 = 提取域名(网址)
        if not 强制:
            缓存项 = self.缓存.查询(域名)
            if 缓存项 is not None:
                self.统计.记录命中()
                return self._缓存响应(缓存项)

        结果 = await self._调用后端(网址)
        缓存项 = self._入库(域名, 结果)
        return {
            "clearance": 缓存项.凭据,
            "user_agent": 缓存项.用户代理,
            "domain": 域名,
            "expires_in": 缓存项.剩余(),
            "cached": False,
            "solve_seconds": round(结果.耗时秒, 2),
            "attempts": [条目.转字典() for 条目 in 结果.尝试记录],
        }

    async def 求解令牌(self, 网址: str, sitekey: str) -> dict[str, Any]:
        """产出一个一次性 Turnstile 令牌。

        令牌首次使用即被消费，因此本方法绝不读写 ``self.缓存``；统计也单独
        计数，避免污染 clearance 的平均求解时间。
        """
        调用 = getattr(self.后端, "求解令牌", None)
        if not callable(调用):
            raise 接口错误(SOLVE_FAILED, "后端链不支持 Turnstile 令牌")
        try:
            结果 = await anyio.to_thread.run_sync(
                调用, 网址, sitekey, self.配置.solve_timeout
            )
        except 求解超时 as 异常:
            self.统计.记录令牌错误()
            raise 接口错误(
                SOLVE_TIMEOUT, str(异常), 附加信息=_尝试附加信息(异常)
            ) from 异常
        except 求解失败 as 异常:
            self.统计.记录令牌错误()
            raise 接口错误(
                SOLVE_FAILED, str(异常), 附加信息=_尝试附加信息(异常)
            ) from 异常
        except Exception as 异常:
            self.统计.记录令牌错误()
            日志.exception("后端求解 Turnstile 时抛出未预期错误")
            raise 接口错误(SOLVE_FAILED, f"后端异常：{异常}") from 异常

        self.统计.记录令牌求解()
        return {
            "want": "turnstile",
            "token": 结果.令牌,
            "user_agent": 结果.用户代理,
            "domain": 提取域名(网址),
            "solve_seconds": round(结果.耗时秒, 2),
            "cached": False,
        }

    async def 回放(self, 网址: str) -> dict[str, Any]:
        域名 = 提取域名(网址)
        缓存项 = self.缓存.查询(域名)
        已重解 = False
        if 缓存项 is None:
            缓存项 = await self._求解并入库(网址)
            已重解 = True

        结果 = await self._回放一次(网址, 缓存项, 域名)
        if 结果.挑战页:
            # 凭据已死（TTL 到期、IP 漂移或 UA 不匹配）：
            # 删除它并只重解一次，然后放弃。
            日志.info("%s 回放被拦截；重新求解一次", 域名)
            self.缓存.删除(域名)
            缓存项 = await self._求解并入库(网址)
            结果 = await self._回放一次(网址, 缓存项, 域名)
            已重解 = True
            if 结果.挑战页:
                self.统计.记录错误()
                raise 接口错误(
                    UPSTREAM_403,
                    f"重新求解后目标仍返回拦截页"
                    f"（状态 {结果.状态码}，{结果.长度} 字节）",
                )

        return {
            "url": 结果.网址,
            "domain": 域名,
            "status": 结果.状态码,
            "length": 结果.长度,
            "challenge_page": 结果.挑战页,
            "replay_seconds": 结果.耗时秒,
            "resolved": 已重解,
        }

    def _缓存响应(self, 缓存项: 缓存凭据) -> dict[str, Any]:
        return {
            "clearance": 缓存项.凭据,
            "user_agent": 缓存项.用户代理,
            "domain": 缓存项.域名,
            "expires_in": 缓存项.剩余(),
            "cached": True,
            "solve_seconds": 0.0,
            "attempts": [],
        }

    async def _调用后端(self, 网址: str) -> 放行结果:
        try:
            结果 = await anyio.to_thread.run_sync(
                self.后端.求解, 网址, self.配置.solve_timeout
            )
        except 求解超时 as 异常:
            self.统计.记录错误()
            raise 接口错误(
                SOLVE_TIMEOUT, str(异常), 附加信息=_尝试附加信息(异常)
            ) from 异常
        except 求解失败 as 异常:
            self.统计.记录错误()
            raise 接口错误(
                SOLVE_FAILED, str(异常), 附加信息=_尝试附加信息(异常)
            ) from 异常
        except Exception as 异常:
            self.统计.记录错误()
            日志.exception("后端抛出未预期错误")
            raise 接口错误(SOLVE_FAILED, f"后端异常：{异常}") from 异常
        self.统计.记录求解(结果.耗时秒)
        return 结果

    async def _求解并入库(self, 网址: str) -> 缓存凭据:
        结果 = await self._调用后端(网址)
        return self._入库(提取域名(网址), 结果)

    def _入库(self, 域名: str, 结果: 放行结果) -> 缓存凭据:
        剩余秒数 = max(1, min(self.配置.ttl_seconds, 结果.剩余秒数))
        return self.缓存.写入(
            域名, 结果.凭据, 结果.用户代理, 有效期=剩余秒数
        )

    async def _回放一次(
        self, 网址: str, 缓存项: 缓存凭据, 域名: str
    ) -> 回放结果:
        调用 = functools.partial(
            回放,
            网址,
            缓存项.凭据,
            缓存项.用户代理,
            域名,
            超时=self.配置.replay_timeout,
        )
        try:
            return await anyio.to_thread.run_sync(调用)
        except ValueError as 异常:
            raise 接口错误(BAD_REQUEST, str(异常)) from 异常
        except Exception as 异常:
            self.统计.记录错误()
            raise 接口错误(UPSTREAM_403, f"回放请求失败：{异常}") from 异常


def 校验网址(原始值: str) -> str:
    网址 = 原始值.strip()
    if not 网址 or len(网址) > 2048:
        raise 接口错误(BAD_REQUEST, "url 长度必须为 1-2048 个字符")
    片段 = urlsplit(网址)
    if 片段.scheme not in ("http", "https") or not 片段.hostname:
        raise 接口错误(BAD_REQUEST, "url 必须是绝对的 http(s) 网址")
    return 网址


站点密钥模式 = re.compile(r"^0x[0-9a-zA-Z]{20,}$")


def 校验站点密钥(原始值: str | None) -> str:
    """校验 sitekey 的格式。

    我们无法离线验证 sitekey 注册在哪个域名，所以只保证请求本身自洽：
    网址是绝对的 http(s)，sitekey 形如 Cloudflare 的 0x 开头串。
    两项都在启动浏览器之前完成，失败即快速返回 400。
    """
    值 = (原始值 or "").strip()
    if not 值:
        raise 接口错误(BAD_REQUEST, "want=turnstile 时必须提供 sitekey")
    if 站点密钥模式.fullmatch(值) is None:
        raise 接口错误(
            BAD_REQUEST, "sitekey 必须是以 0x 开头的 20 位以上字母数字"
        )
    return 值


async def 校验令牌(请求: Request) -> str:
    服务实例: 服务 = 请求.app.state.服务
    方案, _, 令牌 = 请求.headers.get("authorization", "").partition(" ")
    令牌 = 令牌.strip()
    if 方案.lower() != "bearer" or not 令牌 or not secrets.compare_digest(
        令牌, 服务实例.配置.auth_token
    ):
        raise 接口错误(AUTH_MISSING, "Bearer 令牌缺失或无效")
    return 令牌


鉴权 = Annotated[str, Depends(校验令牌)]


def _尝试附加信息(异常: BaseException) -> dict[str, object]:
    尝试记录 = getattr(异常, "尝试记录", None)
    if not 尝试记录:
        return {}
    return {
        "attempts": [
            条目.转字典() if isinstance(条目, 单次尝试) else 条目 for 条目 in 尝试记录
        ]
    }


def 构建后端(配置对象: 配置) -> 后端:
    """构建配置指定的链路。未知名称会被跳过，而不是致命错误。"""
    链路: list[后端] = []
    for 名称 in 配置对象.后端名称列表:
        if 名称 == "camoufox":
            链路.append(
                Camoufox后端(
                    无头=配置对象.headless,
                    挑战超时=配置对象.challenge_timeout,
                    轮询间隔=配置对象.poll_interval,
                )
            )
        elif 名称 == "pydoll":
            链路.append(
                Pydoll后端(
                    无头=配置对象.headless,
                    挑战超时=配置对象.challenge_timeout,
                    轮询间隔=配置对象.poll_interval,
                    Chrome路径=配置对象.chrome_path or None,
                )
            )
        else:
            日志.warning("CF_SLAYER_BACKENDS 中出现未知后端 %r；已忽略", 名称)
    if not 链路:
        raise RuntimeError(
            "没有可用后端；请设置 CF_SLAYER_BACKENDS=camoufox,pydoll"
        )
    return 后端链(链路)


def 创建应用(
    配置对象: 配置 | None = None,
    后端对象: 后端 | None = None,
    缓存: SQLite缓存 | None = None,
) -> FastAPI:
    配置对象 = 配置对象 or 加载配置()
    if not 配置对象.auth_token:
        raise RuntimeError("auth_token 为空；请使用 加载配置() 或显式传入")
    后端对象 = 后端对象 or 构建后端(配置对象)
    缓存 = 缓存 or SQLite缓存(配置对象.cache_path, 配置对象.ttl_seconds)
    服务实例 = 服务(配置对象, 缓存, 后端对象)
    预热器实例: 预热器 | None = None
    if 配置对象.预热目标列表:
        预热器实例 = 预热器(
            服务实例,
            配置对象.预热目标列表,
            余量秒=配置对象.prewarm_margin,
            间隔=配置对象.prewarm_interval,
        )

    @asynccontextmanager
    async def 生命周期(_: FastAPI) -> AsyncIterator[None]:
        停止信号 = asyncio.Event()
        任务: asyncio.Task[None] | None = None
        if 预热器实例 is not None:
            任务 = asyncio.create_task(预热器实例.运行(停止信号))
        try:
            yield
        finally:
            停止信号.set()
            if 任务 is not None:
                try:
                    await asyncio.wait_for(任务, timeout=5)
                except TimeoutError:
                    任务.cancel()
                    with suppress(asyncio.CancelledError):
                        await 任务
            缓存.关闭()

    应用 = FastAPI(title="cf-slayer", version=__version__, lifespan=生命周期)
    应用.state.服务 = 服务实例
    应用.state.预热器 = 预热器实例

    @应用.exception_handler(接口错误)
    async def _接口异常(_: Request, 异常: 接口错误) -> JSONResponse:
        return JSONResponse(异常.响应体(), status_code=异常.状态码)

    @应用.exception_handler(RequestValidationError)
    async def _请求校验异常(_: Request, 异常: RequestValidationError) -> JSONResponse:
        详情 = "；".join(
            f"{'.'.join(str(部分) for 部分 in 错误['loc'])}: {错误['msg']}"
            for 错误 in 异常.errors()
        )
        return JSONResponse(
            接口错误(BAD_REQUEST, f"请求无效：{详情}").响应体(), status_code=400
        )

    @应用.exception_handler(Exception)
    async def _未处理异常(_: Request, 异常: Exception) -> JSONResponse:
        日志.exception("未处理错误", exc_info=异常)
        return JSONResponse(
            接口错误(SOLVE_FAILED, "内部错误", 状态码=500).响应体(),
            status_code=500,
        )

    @应用.post("/solve")
    async def 求解(请求体: 求解请求, _: 鉴权) -> dict[str, Any]:
        网址 = 校验网址(请求体.url)
        if 请求体.want == "clearance":
            return await 服务实例.求解(网址)
        if 请求体.want == "turnstile":
            return await 服务实例.求解令牌(网址, 校验站点密钥(请求体.sitekey))
        raise 接口错误(
            BAD_REQUEST,
            f"不支持的 want={请求体.want!r}；仅支持 clearance 或 turnstile",
        )

    @应用.get("/replay")
    async def 回放目标(url: Annotated[str, Query()], _: 鉴权) -> dict[str, Any]:
        return await 服务实例.回放(校验网址(url))

    @应用.get("/health")
    async def 健康检查() -> dict[str, str]:
        return {"state": "ready", "browser": 服务实例.后端.名称}

    @应用.get("/stats")
    async def 统计信息(_: 鉴权) -> dict[str, int | float]:
        快照 = 服务实例.统计.快照()
        快照["cached_domains"] = 服务实例.缓存.计数()
        return 快照

    return 应用


_应用实例: FastAPI | None = None


def __getattr__(属性名: str) -> Any:
    """惰性构建 ASGI 应用，使导入本模块没有副作用。"""
    if 属性名 == "app":
        global _应用实例
        if _应用实例 is None:
            _应用实例 = 创建应用()
        return _应用实例
    raise AttributeError(f"module {__name__!r} has no attribute {属性名!r}")
