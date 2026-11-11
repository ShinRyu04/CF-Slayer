"""基于 Camoufox 的求解后端。``后端`` 是未来接入其他引擎的接口层。

每次求解启动一个浏览器，v1 不做连接池。本模块全部同步执行；
FastAPI 层会在工作线程里调用。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .errors import 求解失败, 求解超时

日志 = logging.getLogger(__name__)

默认有效期秒数 = 1800

浏览器工厂 = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True, slots=True)
class 单次尝试:
    """链路中的一次后端尝试。仅当 ``成功`` 为真时 ``错误`` 才为 None。"""

    后端名: str
    成功: bool
    错误: str | None
    秒数: float

    def 转字典(self) -> dict[str, Any]:
        return {
            "backend": self.后端名,
            "ok": self.成功,
            "error": self.错误,
            "seconds": round(self.秒数, 3),
        }


@dataclass(frozen=True, slots=True)
class 放行结果:
    凭据: str
    用户代理: str
    域名: str
    剩余秒数: int
    耗时秒: float
    尝试记录: tuple[单次尝试, ...] = ()


@runtime_checkable
class 后端(Protocol):
    """把网址变成一个 ``cf_clearance`` 凭据的后端。

    实现必须抛出 ``求解超时`` / ``求解失败``，而不是返回占位值，
    这样 HTTP 层才能精确映射错误码。
    """

    名称: str

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果: ...


def 提取域名(网址: str) -> str:
    """缓存键：小写主机名，不含端口，不含协议。"""
    主机名 = urlsplit(网址).hostname or ""
    return 主机名.lower().rstrip(".")


def _默认浏览器工厂(无头: bool) -> 浏览器工厂:
    def 工厂() -> AbstractContextManager[Any]:
        # 延迟导入，保证在未下载 700 MB Camoufox 火狐包时
        # 服务仍可导入（测试也可运行）。
        from camoufox.sync_api import Camoufox

        return Camoufox(headless=无头, geoip=False)

    return 工厂


class Camoufox后端:
    名称 = "camoufox"

    def __init__(
        self,
        *,
        无头: bool = True,
        挑战超时: float = 90.0,
        轮询间隔: float = 2.0,
        工厂: 浏览器工厂 | None = None,
    ) -> None:
        if 挑战超时 <= 0 or 轮询间隔 <= 0:
            raise ValueError("挑战超时与轮询间隔必须为正数")
        self._挑战超时 = 挑战超时
        self._轮询间隔 = 轮询间隔
        self._浏览器工厂 = 工厂 or _默认浏览器工厂(无头)

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        if 超时 <= 0:
            raise ValueError("超时必须为正数")
        开始时刻 = time.monotonic()
        截止时刻 = 开始时刻 + 超时
        挑战截止时刻 = min(截止时刻, 开始时刻 + self._挑战超时)
        域名 = 提取域名(网址)
        if not 域名:
            raise 求解失败(f"无法从 {网址!r} 解析出域名")

        try:
            with self._浏览器工厂() as 浏览器:
                上下文 = 浏览器.new_context()
                try:
                    页面 = 上下文.new_page()
                    跳转毫秒 = int(max(1.0, min(45.0, 截止时刻 - time.monotonic())) * 1000)
                    页面.goto(网址, wait_until="domcontentloaded", timeout=跳转毫秒)
                    凭据, 用户代理, 剩余秒数 = self._等待放行(
                        页面, 上下文, 挑战截止时刻, 截止时刻, 网址
                    )
                finally:
                    self._安静关闭(上下文)
        except (求解超时, 求解失败):
            raise
        except Exception as 异常:  # 浏览器崩溃、导航失败、启动参数错误
            if time.monotonic() >= 截止时刻:
                raise 求解超时(f"{域名} 的求解超过 {超时:.0f} 秒") from 异常
            raise 求解失败(f"camoufox 异常：{type(异常).__name__}: {异常}") from 异常

        return 放行结果(
            凭据=凭据,
            用户代理=用户代理,
            域名=域名,
            剩余秒数=剩余秒数,
            耗时秒=round(time.monotonic() - 开始时刻, 3),
        )

    def _等待放行(
        self,
        页面: Any,
        上下文: Any,
        挑战截止时刻: float,
        截止时刻: float,
        网址: str,
    ) -> tuple[str, str, int]:
        while True:
            饼干 = self._查找放行(页面, 上下文)
            if 饼干 is not None:
                用户代理 = self._读取用户代理(页面)
                if 用户代理:
                    剩余秒数 = _计算剩余秒数(饼干)
                    if 剩余秒数 is not None:
                        return str(饼干["value"]), 用户代理, 剩余秒数
            当前时刻 = time.monotonic()
            if 当前时刻 >= 挑战截止时刻 or 当前时刻 >= 截止时刻:
                raise 求解超时(
                    f"{网址} 在 {self._挑战超时:.0f} 秒内未出现 cf_clearance"
                )
            页面.wait_for_timeout(int(self._轮询间隔 * 1000))

    def _查找放行(self, 页面: Any, 上下文: Any) -> dict[str, Any] | None:
        for 饼干 in self._收集饼干(页面, 上下文):
            if 饼干.get("name") == "cf_clearance" and 饼干.get("value"):
                return 饼干
        return None

    def _收集饼干(self, 页面: Any, 上下文: Any) -> list[dict[str, Any]]:
        饼干列表: list[dict[str, Any]] = []
        try:
            饼干列表.extend(上下文.cookies())
        except Exception as 异常:
            日志.debug("context.cookies() 失败：%s", 异常)
        # document.cookie 看不到 HttpOnly 饼干，但读取成本为零，
        # 可覆盖那些以非 HttpOnly 方式下发 cf_clearance 的站点。
        try:
            原文 = 页面.evaluate("() => document.cookie") or ""
            for 片段 in str(原文).split(";"):
                名称, 分隔符, 值 = 片段.strip().partition("=")
                if 分隔符 and 名称:
                    饼干列表.append({"name": 名称, "value": 值})
        except Exception as 异常:
            日志.debug("读取 document.cookie 失败：%s", 异常)
        return 饼干列表

    def _读取用户代理(self, 页面: Any) -> str:
        try:
            值 = 页面.evaluate("() => navigator.userAgent")
        except Exception as 异常:
            日志.debug("读取 navigator.userAgent 失败：%s", 异常)
            return ""
        return str(值) if isinstance(值, str) else ""

    @staticmethod
    def _安静关闭(上下文: Any) -> None:
        try:
            上下文.close()
        except Exception as 异常:
            日志.debug("context.close() 失败：%s", 异常)


def _计算剩余秒数(饼干: dict[str, Any]) -> int | None:
    """剩余有效秒数；``None`` 表示这个饼干已经失效。"""
    过期时间 = 饼干.get("expires")
    if not isinstance(过期时间, int | float) or 过期时间 <= 0:
        return 默认有效期秒数
    剩余 = int(过期时间 - time.time())
    return 剩余 if 剩余 > 0 else None
