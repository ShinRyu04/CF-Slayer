"""后台预热：在凭据过期前重新求解域名。

当 ``CF_SLAYER_PREWARM_DOMAINS`` 非空时由应用生命周期启动。
每一轮检查缓存，只处理缺失或距过期不足 ``预热余量`` 秒的记录。
按域名的“处理中”集合避免多轮之间重复求解。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .engine import 提取域名
from .errors import 接口错误

if TYPE_CHECKING:
    from .main import 服务

日志 = logging.getLogger(__name__)

默认间隔 = 60.0
默认余量 = 300


def 规范化目标(目标: str) -> str:
    """``steamdb.info`` -> ``https://steamdb.info/``；完整 URL 原样返回。"""
    目标 = 目标.strip()
    if not 目标:
        return ""
    if "://" in 目标:
        return 目标
    主机名 = 目标.rstrip("/")
    if urlsplit(主机名).netloc:
        return f"https://{主机名}/"
    return f"https://{主机名}/"


class 预热器:
    def __init__(
        self,
        服务: 服务,
        目标列表: Sequence[str],
        *,
        余量秒: int = 默认余量,
        间隔: float = 默认间隔,
        当前时刻: Callable[[], float] = time.time,
    ) -> None:
        self._服务 = 服务
        self._目标列表 = tuple(
            网址
            for 网址 in (规范化目标(目标) for 目标 in 目标列表)
            if 提取域名(网址)
        )
        self._余量 = max(0, 余量秒)
        self._间隔 = 间隔
        self._当前时刻 = 当前时刻
        self._处理中: set[str] = set()

    @property
    def 目标列表(self) -> tuple[str, ...]:
        return self._目标列表

    async def 轮询一轮(self) -> None:
        for 网址 in self._目标列表:
            域名 = 提取域名(网址)
            if not 域名 or 域名 in self._处理中:
                日志.debug("预热跳过 %s（已在求解）", 域名 or 网址)
                continue
            缓存项 = self._服务.缓存.查询(域名, 当前时刻=self._当前时刻())
            if 缓存项 is not None and 缓存项.剩余(self._当前时刻()) >= self._余量:
                continue

            self._处理中.add(域名)
            try:
                结果 = await self._服务.求解(网址, 强制=True)
                日志.info(
                    "预热完成 %s，用时 %s 秒（尝试 %d 次）",
                    域名,
                    结果.get("solve_seconds"),
                    len(结果.get("attempts") or ()),
                )
            except asyncio.CancelledError:
                raise
            except 接口错误 as 异常:
                self._服务.统计.记录预热错误()
                日志.warning(
                    "预热 %s 失败：%s（%s）", 域名, 异常.消息, 异常.代码
                )
            except Exception:  # 预热失败绝不能杀掉后台任务
                self._服务.统计.记录预热错误()
                日志.exception("预热 %s 崩溃", 域名)
            finally:
                self._处理中.discard(域名)

    async def 运行(self, 停止信号: asyncio.Event | None = None) -> None:
        日志.info(
            "预热已启动：%s（间隔 %s 秒，余量 %s 秒）",
            "、".join(self._目标列表),
            self._间隔,
            self._余量,
        )
        while 停止信号 is None or not 停止信号.is_set():
            try:
                await self.轮询一轮()
            except asyncio.CancelledError:
                raise
            except Exception:  # 保持循环存活
                self._服务.统计.记录预热错误()
                日志.exception("预热轮询失败")
            if 停止信号 is None:
                await asyncio.sleep(self._间隔)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(停止信号.wait(), timeout=self._间隔)
        日志.info("预热已停止")
