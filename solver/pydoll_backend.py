"""Pydoll（CDP）后端：直接驱动已安装的 Chrome/Chromium。

Pydoll 通过 DevTools 协议与 Chrome 通信，不需要 WebDriver。浏览器在求解时
才被探测；若不存在，链会跳过该后端，而不是让请求失败。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .engine import 提取域名, 放行结果
from .errors import 求解失败, 求解超时

日志 = logging.getLogger(__name__)

Chrome候选路径 = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
Chrome命令名 = ("chrome", "chrome.exe", "chromium", "chromium.exe", "google-chrome")


def 查找Chrome(指定路径: str | None = None) -> str | None:
    """返回可用的 Chrome/Chromium 路径，找不到则返回 ``None``。

    ``指定路径``（CF_SLAYER_CHROME_PATH）优先；若它指向不存在的文件，
    不会静默回退——那样会掩盖拼写错误。
    """
    if 指定路径:
        return 指定路径 if Path(指定路径).is_file() else None
    for 候选路径 in Chrome候选路径:
        if Path(候选路径).is_file():
            return 候选路径
    for 命令名 in Chrome命令名:
        命中路径 = shutil.which(命令名)
        if 命中路径:
            return 命中路径
    return None


def Chrome主版本(Chrome路径: str) -> str | None:
    """Windows 上 Chrome 把构建放在 ``Application/<版本>/`` 目录下。"""
    匹配 = re.match(r"(\d+)\.", Path(Chrome路径).parent.name)
    return 匹配.group(1) if 匹配 else None


def Chrome用户代理(Chrome路径: str) -> str | None:
    """与已安装版本匹配的非无头 UA。

    Chrome 默认会在 UA 里写 ``HeadlessChrome``，这是 Cloudflare 的即时信号。
    ``--user-agent`` 同时让 navigator.userAgent 与网络头保持一致。
    """
    主版本 = Chrome主版本(Chrome路径)
    if 主版本 is None:
        return None
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{主版本}.0.0.0 Safari/537.36"
    )


class Pydoll后端:
    名称 = "pydoll"

    def __init__(
        self,
        *,
        无头: bool = True,
        挑战超时: float = 90.0,
        轮询间隔: float = 2.0,
        Chrome路径: str | None = None,
        用户代理: str | None = None,
    ) -> None:
        if 挑战超时 <= 0 or 轮询间隔 <= 0:
            raise ValueError("挑战超时与轮询间隔必须为正数")
        self._无头 = 无头
        self._挑战超时 = 挑战超时
        self._轮询间隔 = 轮询间隔
        self._Chrome路径 = Chrome路径
        self._用户代理 = 用户代理

    def 可用(self) -> tuple[bool, str]:
        if importlib.util.find_spec("pydoll") is None:
            return False, "未安装 pydoll（pip install pydoll-python）"
        路径 = self._解析Chrome()
        if 路径 is None:
            if self._Chrome路径:
                return False, f"未在 CF_SLAYER_CHROME_PATH={self._Chrome路径!r} 找到 Chrome"
            return False, "未找到 Chrome/Chromium（常见安装路径与 PATH 均无）"
        return True, ""

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        可用, 原因 = self.可用()
        if not 可用:
            raise 求解失败(原因)
        if 超时 <= 0:
            raise ValueError("超时必须为正数")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise 求解失败("pydoll 后端必须在工作线程中运行（检测到事件循环）")

        开始时刻 = time.monotonic()
        try:
            凭据, 用户代理, 剩余秒数 = asyncio.run(
                self._异步求解(网址, 超时, 开始时刻)
            )
        except (求解失败, 求解超时):
            raise
        except Exception as 异常:
            raise 求解失败(f"pydoll 异常：{type(异常).__name__}: {异常}") from 异常
        return 放行结果(
            凭据=凭据,
            用户代理=用户代理,
            域名=提取域名(网址),
            剩余秒数=剩余秒数,
            耗时秒=round(time.monotonic() - 开始时刻, 3),
        )

    def _解析Chrome(self) -> str | None:
        return 查找Chrome(self._Chrome路径)

    async def _异步求解(
        self, 网址: str, 超时: float, 开始时刻: float
    ) -> tuple[str, str, int]:
        from pydoll.browser import Chrome
        from pydoll.browser.options import ChromiumOptions

        Chrome路径 = self._解析Chrome()
        if Chrome路径 is None:
            raise 求解失败("未找到 Chrome/Chromium")
        截止时刻 = 开始时刻 + min(超时, self._挑战超时)

        选项 = ChromiumOptions()
        选项.headless = self._无头
        选项.binary_location = Chrome路径
        选项.add_argument("--no-first-run")
        选项.add_argument("--no-default-browser-check")
        选项.add_argument("--disable-blink-features=AutomationControlled")
        用户代理 = self._用户代理 or Chrome用户代理(Chrome路径)
        if 用户代理:
            选项.add_argument(f"--user-agent={用户代理}")

        async with Chrome(options=选项) as 浏览器:
            标签页 = await 浏览器.start()
            跳转超时 = int(max(1.0, min(45.0, 截止时刻 - time.monotonic())))
            await 标签页.go_to(网址, timeout=跳转超时)
            return await self._轮询凭据(标签页, 截止时刻, 网址)

    async def _轮询凭据(
        self, 标签页: Any, 截止时刻: float, 网址: str
    ) -> tuple[str, str, int]:
        while True:
            饼干 = await self._查找饼干(标签页)
            if 饼干 is not None:
                用户代理 = await self._读取用户代理(标签页)
                if 用户代理:
                    return str(饼干["value"]), 用户代理, _计算剩余秒数(饼干)
            if time.monotonic() >= 截止时刻:
                raise 求解超时(
                    f"{网址} 在 {self._挑战超时:.0f} 秒内未出现 cf_clearance"
                )
            await asyncio.sleep(self._轮询间隔)

    async def _查找饼干(self, 标签页: Any) -> dict[str, Any] | None:
        try:
            饼干列表 = await 标签页.get_cookies()
        except Exception as 异常:  # 标签页可能正在导航
            日志.debug("pydoll get_cookies 失败：%s", 异常)
            return None
        for 饼干 in 饼干列表 or ():
            if 饼干.get("name") == "cf_clearance" and 饼干.get("value"):
                return dict(饼干)
        return None

    async def _读取用户代理(self, 标签页: Any) -> str:
        try:
            响应 = await 标签页.execute_script(
                "navigator.userAgent", return_by_value=True
            )
        except Exception as 异常:  # 执行上下文可能已被销毁
            日志.debug("pydoll 读取 userAgent 失败：%s", 异常)
            return ""
        值 = 响应.get("result", {}).get("result", {}).get("value")
        return 值 if isinstance(值, str) else ""


def _计算剩余秒数(饼干: dict[str, Any]) -> int:
    过期时间 = 饼干.get("expires")
    if not isinstance(过期时间, int | float) or 过期时间 <= 0:
        return 1800
    剩余 = int(过期时间 - time.time())
    return 剩余 if 剩余 > 0 else 1800
