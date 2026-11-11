"""用浏览器求解时所用的完全相同的 UA 回放凭据。

研究得出的硬性规则：同出口 IP、同 UA、约 30 分钟 TTL、按域名隔离。
本模块负责强制 UA 部分；IP/TTL/域名由调用方（缓存键与过期时间、同主机）保证。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: 用于识别 Cloudflare 拦截页而非真实内容的子串。
挑战标记 = (
    b"just a moment",
    b"cf-chl",
    b"challenge-platform",
    b"attention required",
    b"enable javascript and cookies to continue",
)

#: 只扫描前一段；挑战页很小，而真实页面的 JS 包可能在其他文本里包含这些标记。
_扫描字节数 = 262_144


@dataclass(frozen=True, slots=True)
class 回放结果:
    状态码: int
    长度: int
    挑战页: bool
    耗时秒: float
    网址: str


def 是挑战页(状态码: int, 正文: bytes) -> bool:
    """只看状态码会漏掉 Cloudflare 的 200 + 拦截页变体。"""
    if 状态码 in (403, 503):
        return True
    开头 = 正文[:_扫描字节数].lower()
    return any(标记 in 开头 for 标记 in 挑战标记)


def 回放(
    网址: str,
    凭据: str,
    用户代理: str,
    域名: str = "",
    *,
    超时: float = 30.0,
    会话工厂: Any | None = None,
) -> 回放结果:
    """带上 ``cf_clearance`` 与求解时 UA GET ``网址``。

    饼干按目标网址的 host-only cookie 发送，这是对目标域名及其子域仍有效的
    最小作用域。
    """
    if not 凭据:
        raise ValueError("凭据不能为空")
    if not 用户代理:
        raise ValueError("用户代理不能为空")
    if 域名 and 提取域名(网址) != 域名.lower().rstrip("."):
        raise ValueError(f"{域名!r} 的凭据不能用于回放 {网址!r}")

    开始时刻 = time.monotonic()
    if 会话工厂 is None:
        from curl_cffi import requests as cffi_requests

        def 会话工厂() -> Any:  # type: ignore[misc]
            return cffi_requests.Session(impersonate="firefox")

    with 会话工厂() as 会话:
        响应 = 会话.get(
            网址,
            headers={
                "User-Agent": 用户代理,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies={"cf_clearance": 凭据},
            timeout=超时,
        )
        正文: bytes = 响应.content or b""
        状态码 = int(响应.status_code)
        最终网址 = str(getattr(响应, "url", 网址) or 网址)

    return 回放结果(
        状态码=状态码,
        长度=len(正文),
        挑战页=是挑战页(状态码, 正文),
        耗时秒=round(time.monotonic() - 开始时刻, 3),
        网址=最终网址,
    )


def 提取域名(网址: str) -> str:
    主机名 = urlsplit(网址).hostname or ""
    return 主机名.lower().rstrip(".")
