"""基于 Camoufox 的求解后端。``后端`` 是未来接入其他引擎的接口层。

每次求解启动一个浏览器，v1 不做连接池。本模块全部同步执行；
FastAPI 层会在工作线程里调用。
"""

from __future__ import annotations

import json
import logging
import re
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


@dataclass(frozen=True, slots=True)
class 令牌结果:
    """一次 Turnstile 求解的产物。

    令牌是一次性的：消费方必须用同一个 ``用户代理`` 回放，否则 CF 会拒绝。
    这里没有剩余秒数——令牌只需立即用掉，不做续期。
    """

    令牌: str
    用户代理: str
    耗时秒: float


@runtime_checkable
class 后端(Protocol):
    """把网址变成一个 ``cf_clearance`` 凭据的后端。

    实现必须抛出 ``求解超时`` / ``求解失败``，而不是返回占位值，
    这样 HTTP 层才能精确映射错误码。
    """

    名称: str

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果: ...


@runtime_checkable
class 令牌后端(Protocol):
    """能产出 Turnstile 一次性令牌的后端。

    这是独立协议：``后端`` 保持不变，不认识 Turnstile 的旧后端与测试桩
    仍然满足 ``后端``。链路用 ``支持令牌`` 探测能力。
    """

    名称: str

    def 求解令牌(self, 网址: str, sitekey: str, 超时: float = 60.0) -> 令牌结果: ...


def 支持令牌(对象: object) -> bool:
    """后端是否实现了 ``令牌后端``。"""
    return isinstance(对象, 令牌后端)


def 提取域名(网址: str) -> str:
    """缓存键：小写主机名，不含端口，不含协议。"""
    主机名 = urlsplit(网址).hostname or ""
    return 主机名.lower().rstrip(".")


_站点密钥模式 = re.compile(r"^0x[0-9a-zA-Z]{20,}$")

_组件地址 = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"

_输入标签模式 = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_响应名模式 = re.compile(
    r"""\bname\s*=\s*(?:"cf-turnstile-response"|'cf-turnstile-response'|cf-turnstile-response)""",
    re.IGNORECASE,
)
_值模式 = re.compile(
    r"""\bvalue\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)


def 提取令牌(页面正文: str) -> str | None:
    """从页面 HTML 的隐藏输入里取出 Turnstile 令牌。

    纯函数，不依赖浏览器；属性顺序与引号风格都不敏感。
    """
    for 标签 in _输入标签模式.findall(页面正文):
        if _响应名模式.search(标签) is None:
            continue
        匹配 = _值模式.search(标签)
        if 匹配 is None:
            continue
        值 = next((组 for 组 in 匹配.groups() if 组 is not None), "").strip()
        if 值:
            return 值
    return None


_加载组件脚本 = """() => new Promise((resolve) => {
  if (window.turnstile) { resolve("ready"); return; }
  const script = document.createElement("script");
  script.src = """ + json.dumps(_组件地址) + """;
  script.async = true;
  script.onload = () => resolve(window.turnstile ? "ready" : "failed");
  script.onerror = () => resolve("failed");
  document.head.appendChild(script);
  setTimeout(() => resolve(window.turnstile ? "ready" : "failed"), 10000);
})"""

_渲染组件前缀 = """() => {
  if (!window.turnstile) { return "missing"; }
  let box = document.getElementById("cf-slayer-turnstile");
  if (!box) {
    box = document.createElement("div");
    box.id = "cf-slayer-turnstile";
    document.body.appendChild(box);
  }
  window.__cfSlayerToken = "";
  window.turnstile.render("#cf-slayer-turnstile", {
    sitekey: """

_渲染组件后缀 = """,
    callback: (token) => { window.__cfSlayerToken = token || ""; },
    "error-callback": () => { window.__cfSlayerToken = ""; },
  });
  return "rendered";
}"""


def _渲染脚本(sitekey: str) -> str:
    return _渲染组件前缀 + json.dumps(sitekey) + _渲染组件后缀

_读取回调令牌脚本 = '() => window.__cfSlayerToken || ""'


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
        令牌超时: float = 60.0,
        令牌轮询间隔: float = 1.0,
        工厂: 浏览器工厂 | None = None,
    ) -> None:
        if min(挑战超时, 轮询间隔, 令牌超时, 令牌轮询间隔) <= 0:
            raise ValueError("挑战超时、轮询间隔、令牌超时与令牌轮询间隔必须为正数")
        self._挑战超时 = 挑战超时
        self._轮询间隔 = 轮询间隔
        self._令牌超时 = 令牌超时
        self._令牌轮询间隔 = 令牌轮询间隔
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

    def 求解令牌(
        self, 网址: str, sitekey: str, 超时: float = 60.0
    ) -> 令牌结果:
        """打开目标页、显式渲染 Turnstile、轮询隐藏输入直到拿到令牌。

        令牌一次性，因此每次调用都新建页面与上下文，绝不复用。
        """
        if 超时 <= 0:
            raise ValueError("超时必须为正数")
        if not _站点密钥模式.fullmatch(sitekey):
            raise 求解失败(f"sitekey 格式无效：{sitekey!r}")
        开始时刻 = time.monotonic()
        令牌截止时刻 = min(开始时刻 + 超时, 开始时刻 + self._令牌超时)
        域名 = 提取域名(网址)
        if not 域名:
            raise 求解失败(f"无法从 {网址!r} 解析出域名")

        try:
            with self._浏览器工厂() as 浏览器:
                上下文 = 浏览器.new_context()
                try:
                    页面 = 上下文.new_page()
                    跳转毫秒 = int(
                        max(1.0, min(45.0, 令牌截止时刻 - time.monotonic())) * 1000
                    )
                    # 先落在空白页，避免目标站自身的 Turnstile 自动渲染，
                    # 再导航到目标地址，由我们显式渲染同一个 sitekey。
                    页面.goto("about:blank", wait_until="domcontentloaded", timeout=跳转毫秒)
                    页面.goto(网址, wait_until="domcontentloaded", timeout=跳转毫秒)
                    令牌, 用户代理 = self._等待令牌(
                        页面, sitekey, 令牌截止时刻, 网址
                    )
                finally:
                    self._安静关闭(上下文)
        except (求解超时, 求解失败):
            raise
        except Exception as 异常:  # 浏览器崩溃、导航失败、启动参数错误
            if time.monotonic() >= 令牌截止时刻:
                raise 求解超时(
                    f"{域名} 的 Turnstile 求解超过 {self._令牌超时:.0f} 秒"
                ) from 异常
            raise 求解失败(
                f"camoufox Turnstile 异常：{type(异常).__name__}: {异常}"
            ) from 异常

        return 令牌结果(
            令牌=令牌,
            用户代理=用户代理,
            耗时秒=round(time.monotonic() - 开始时刻, 3),
        )

    def _等待令牌(
        self, 页面: Any, sitekey: str, 截止时刻: float, 网址: str
    ) -> tuple[str, str]:
        状态 = 页面.evaluate(_加载组件脚本)
        if 状态 != "ready":
            raise 求解失败(f"Turnstile 组件加载失败（状态 {状态!r}）")

        状态 = 页面.evaluate(_渲染脚本(sitekey))
        if 状态 != "rendered":
            raise 求解失败(f"Turnstile 渲染失败（状态 {状态!r}）")

        while True:
            令牌 = 提取令牌(self._读取正文(页面)) or self._读取回调令牌(页面)
            if 令牌:
                用户代理 = self._读取用户代理(页面)
                if 用户代理:
                    return 令牌, 用户代理
            当前时刻 = time.monotonic()
            if 当前时刻 >= 截止时刻:
                raise 求解超时(
                    f"{网址} 在 {self._令牌超时:.0f} 秒内未产出 Turnstile 令牌"
                )
            # 睡到截止时刻为止，避免最后一次轮询越过时限。
            页面.wait_for_timeout(
                int(max(1.0, min(self._令牌轮询间隔, 截止时刻 - 当前时刻)) * 1000)
            )

    @staticmethod
    def _读取正文(页面: Any) -> str:
        try:
            return str(页面.content() or "")
        except Exception as 异常:
            日志.debug("读取页面正文失败：%s", 异常)
            return ""

    def _读取回调令牌(self, 页面: Any) -> str:
        try:
            值 = 页面.evaluate(_读取回调令牌脚本)
        except Exception as 异常:
            日志.debug("读取 Turnstile 回调令牌失败：%s", 异常)
            return ""
        return str(值).strip() if isinstance(值, str) else ""

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
