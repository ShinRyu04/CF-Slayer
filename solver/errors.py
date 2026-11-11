"""HTTP 层、引擎与回放客户端共用的错误契约。"""

from __future__ import annotations

from collections.abc import Sequence

#: JSON 响应体中暴露的错误码：{"error": {"code": ..., "message": ...}}
SOLVE_TIMEOUT = "SOLVE_TIMEOUT"
SOLVE_FAILED = "SOLVE_FAILED"
UPSTREAM_403 = "UPSTREAM_403"
AUTH_MISSING = "AUTH_MISSING"
BAD_REQUEST = "BAD_REQUEST"

状态码映射 = {
    SOLVE_TIMEOUT: 504,
    SOLVE_FAILED: 502,
    UPSTREAM_403: 502,
    AUTH_MISSING: 401,
    BAD_REQUEST: 400,
}


class 接口错误(Exception):
    """可直接映射为 HTTP 响应的错误。"""

    def __init__(
        self,
        代码: str,
        消息: str,
        状态码: int | None = None,
        附加信息: dict[str, object] | None = None,
    ) -> None:
        super().__init__(消息)
        self.代码 = 代码
        self.消息 = 消息
        self.状态码 = 状态码 if 状态码 is not None else 状态码映射.get(代码, 500)
        self.附加信息 = 附加信息 or {}

    def 响应体(self) -> dict[str, dict[str, object]]:
        正文: dict[str, object] = {"code": self.代码, "message": self.消息}
        正文.update(self.附加信息)
        return {"error": 正文}


class 求解错误(Exception):
    """后端失败。HTTP 层会把它翻译成 API 错误码。"""


class 求解超时(求解错误):
    """后端在产出凭据前耗尽了时间。"""


class 求解失败(求解错误):
    """后端运行了，但没有产出凭据。"""


class 令牌不受支持(求解失败):
    """没有任何后端能产出 Turnstile 令牌。

    ``尝试记录`` 保留每个被跳过后端的原因，便于调用方判断是配置问题
    还是能力问题。
    """

    def __init__(self, 消息: str, 尝试记录: Sequence[object] = ()) -> None:
        super().__init__(消息)
        self.尝试记录 = tuple(尝试记录)
