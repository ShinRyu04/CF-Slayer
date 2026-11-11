"""有序后端回退链。

链路实现了与单个后端相同的 ``后端`` 协议，因此服务层不关心背后有几个求解器。
每次尝试都会记录进 ``放行结果.尝试记录``（或随异常抛出），便于观测。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import replace

from .engine import 令牌结果, 单次尝试, 后端, 支持令牌, 放行结果
from .errors import 令牌不受支持, 求解失败, 求解超时

日志 = logging.getLogger(__name__)


class 链求解失败(求解失败):
    """所有后端都失败；``尝试记录`` 描述每一次尝试。"""

    def __init__(self, 消息: str, 尝试记录: Sequence[单次尝试]) -> None:
        super().__init__(消息)
        self.尝试记录 = tuple(尝试记录)


class 链求解超时(求解超时):
    """所有后端都超时；``尝试记录`` 描述每一次尝试。"""

    def __init__(self, 消息: str, 尝试记录: Sequence[单次尝试]) -> None:
        super().__init__(消息)
        self.尝试记录 = tuple(尝试记录)


class 链令牌失败(求解失败):
    """支持令牌的后端全部失败；``尝试记录`` 含被跳过的后端。"""

    def __init__(self, 消息: str, 尝试记录: Sequence[单次尝试]) -> None:
        super().__init__(消息)
        self.尝试记录 = tuple(尝试记录)


class 链令牌超时(求解超时):
    """支持令牌的后端全部超时；``尝试记录`` 含被跳过的后端。"""

    def __init__(self, 消息: str, 尝试记录: Sequence[单次尝试]) -> None:
        super().__init__(消息)
        self.尝试记录 = tuple(尝试记录)


class 后端链:
    """按顺序尝试后端，返回第一个成功的放行凭据。

    声明自身不可用的后端（``可用()`` 返回 ``(False, 原因)``）会被直接跳过，
    因此没装 Chrome 的机器仍会走 Camoufox，而不是直接崩溃。
    """

    def __init__(self, 后端列表: Sequence[后端]) -> None:
        if not 后端列表:
            raise ValueError("后端链至少需要一个后端")
        self._后端列表 = tuple(后端列表)
        self.名称 = self._后端列表[0].名称

    @property
    def 后端列表(self) -> tuple[后端, ...]:
        return self._后端列表

    def 求解(self, 网址: str, 超时: float = 120.0) -> 放行结果:
        尝试记录: list[单次尝试] = []
        超时次数 = 0

        for 后端对象 in self._后端列表:
            不可用原因 = _探测不可用原因(后端对象)
            if 不可用原因 is not None:
                尝试记录.append(单次尝试(后端对象.名称, False, 不可用原因, 0.0))
                日志.warning("后端 %s 被跳过：%s", 后端对象.名称, 不可用原因)
                continue

            开始时刻 = time.monotonic()
            try:
                结果 = 后端对象.求解(网址, 超时)
            except 求解超时 as 异常:
                超时次数 += 1
                尝试记录.append(
                    单次尝试(后端对象.名称, False, str(异常), time.monotonic() - 开始时刻)
                )
                日志.warning(
                    "后端 %s 超时，用时 %.1f 秒", 后端对象.名称, 尝试记录[-1].秒数
                )
                continue
            except 求解失败 as 异常:
                尝试记录.append(
                    单次尝试(后端对象.名称, False, str(异常), time.monotonic() - 开始时刻)
                )
                日志.warning("后端 %s 失败：%s", 后端对象.名称, 异常)
                continue
            except Exception as 异常:  # 单个后端损坏不能拖垮整条链
                尝试记录.append(
                    单次尝试(
                        后端对象.名称,
                        False,
                        f"{type(异常).__name__}: {异常}",
                        time.monotonic() - 开始时刻,
                    )
                )
                日志.exception("后端 %s 抛出未预期异常", 后端对象.名称)
                continue

            尝试记录.append(
                单次尝试(后端对象.名称, True, None, time.monotonic() - 开始时刻)
            )
            日志.info("后端 %s 求解成功：%s", 后端对象.名称, 网址)
            return replace(结果, 尝试记录=tuple(尝试记录))

        摘要 = "；".join(f"{条目.后端名}: {条目.错误}" for 条目 in 尝试记录) or "没有后端执行"
        消息 = f"所有后端均失败（共 {len(尝试记录)} 次尝试）：{摘要}"
        if 尝试记录 and 超时次数 == len(尝试记录):
            raise 链求解超时(消息, 尝试记录)
        raise 链求解失败(消息, 尝试记录)

    def 求解令牌(
        self, 网址: str, sitekey: str, 超时: float = 60.0
    ) -> 令牌结果:
        尝试记录: list[单次尝试] = []
        超时次数 = 0
        支持数 = 0

        for 后端对象 in self._后端列表:
            if not 支持令牌(后端对象):
                尝试记录.append(
                    单次尝试(后端对象.名称, False, "该后端不支持 Turnstile 令牌", 0.0)
                )
                日志.debug("后端 %s 被跳过：不支持 Turnstile", 后端对象.名称)
                continue
            支持数 += 1

            不可用原因 = _探测不可用原因(后端对象)
            if 不可用原因 is not None:
                尝试记录.append(单次尝试(后端对象.名称, False, 不可用原因, 0.0))
                日志.warning("后端 %s 被跳过：%s", 后端对象.名称, 不可用原因)
                continue

            开始时刻 = time.monotonic()
            try:
                结果 = 后端对象.求解令牌(网址, sitekey, 超时)
            except 求解超时 as 异常:
                超时次数 += 1
                尝试记录.append(
                    单次尝试(后端对象.名称, False, str(异常), time.monotonic() - 开始时刻)
                )
                日志.warning(
                    "后端 %s 求解 Turnstile 超时，用时 %.1f 秒",
                    后端对象.名称,
                    尝试记录[-1].秒数,
                )
                continue
            except 求解失败 as 异常:
                尝试记录.append(
                    单次尝试(后端对象.名称, False, str(异常), time.monotonic() - 开始时刻)
                )
                日志.warning("后端 %s 求解 Turnstile 失败：%s", 后端对象.名称, 异常)
                continue
            except Exception as 异常:  # 单个后端损坏不能拖垮整条链
                尝试记录.append(
                    单次尝试(
                        后端对象.名称,
                        False,
                        f"{type(异常).__name__}: {异常}",
                        time.monotonic() - 开始时刻,
                    )
                )
                日志.exception("后端 %s 抛出未预期异常", 后端对象.名称)
                continue

            尝试记录.append(
                单次尝试(后端对象.名称, True, None, time.monotonic() - 开始时刻)
            )
            日志.info("后端 %s 产出 Turnstile 令牌：%s", 后端对象.名称, 网址)
            return 结果

        摘要 = "；".join(f"{条目.后端名}: {条目.错误}" for 条目 in 尝试记录) or "没有后端执行"
        if 支持数 == 0:
            raise 令牌不受支持(
                f"没有后端支持 Turnstile 令牌（共 {len(尝试记录)} 个后端）：{摘要}",
                尝试记录,
            )
        消息 = f"所有支持 Turnstile 的后端均失败（共 {len(尝试记录)} 次尝试）：{摘要}"
        if 超时次数 and 超时次数 == 支持数:
            raise 链令牌超时(消息, 尝试记录)
        raise 链令牌失败(消息, 尝试记录)


def _探测不可用原因(后端对象: 后端) -> str | None:
    探测 = getattr(后端对象, "可用", None)
    if not callable(探测):
        return None
    try:
        可用, 原因 = 探测()
    except Exception as 异常:  # 可用性探测绝不能中断调度
        return f"可用性检查失败：{type(异常).__name__}: {异常}"
    return None if 可用 else str(原因 or "不可用")
