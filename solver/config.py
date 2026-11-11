"""运行时配置，以及首次启动时的 Bearer 令牌生成。"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import suppress
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

日志 = logging.getLogger(__name__)

令牌环境变量 = "CF_SLAYER_AUTH_TOKEN"
默认配置文件 = Path(".env")


class 配置(BaseSettings):
    """所有字段映射到 ``CF_SLAYER_<FIELD>`` 环境变量 / .env 键。"""

    model_config = SettingsConfigDict(env_prefix="CF_SLAYER_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8878
    ttl_seconds: int = 1800
    solve_timeout: float = 120.0
    challenge_timeout: float = 90.0
    poll_interval: float = 2.0
    replay_timeout: float = 30.0
    cache_path: str = "cache.sqlite3"
    headless: bool = True
    auth_token: str = ""

    # v2：有序后端链与预热。
    backends: str = "camoufox,pydoll"
    chrome_path: str = ""
    prewarm_domains: str = ""
    prewarm_margin: int = 300
    prewarm_interval: float = 60.0

    @property
    def 后端名称列表(self) -> list[str]:
        return [片段.strip().lower() for 片段 in self.backends.split(",") if 片段.strip()]

    @property
    def 预热目标列表(self) -> list[str]:
        return [片段.strip() for 片段 in self.prewarm_domains.split(",") if 片段.strip()]


def 确保令牌(环境文件: Path = 默认配置文件, 配置对象: 配置 | None = None) -> str:
    """返回 Bearer 令牌；首次运行时生成一个并写入 ``.env``。

    优先级：进程环境变量 > ``.env`` > 新生成。
    """
    环境令牌 = os.environ.get(令牌环境变量, "").strip()
    if 环境令牌:
        return 环境令牌
    if 配置对象 is not None and 配置对象.auth_token.strip():
        return 配置对象.auth_token.strip()

    令牌 = secrets.token_urlsafe(32)
    try:
        现有内容 = 环境文件.read_text(encoding="utf-8") if 环境文件.exists() else ""
        if 现有内容 and not 现有内容.endswith("\n"):
            现有内容 += "\n"
        环境文件.write_text(
            f"{现有内容}{令牌环境变量}={令牌}\n", encoding="utf-8", newline="\n"
        )
        # POSIX 权限在 Windows 上只是建议值；真正起作用的是下面的 ACL。
        with suppress(OSError):
            os.chmod(环境文件, 0o600)
        日志.info("已生成 Bearer 令牌并写入 %s（已请求 chmod 600）", 环境文件)
        日志.info(
            "Windows 不强制 POSIX 权限；请用以下命令收紧文件权限："
            'icacls .env /inheritance:r /grant:r "%%USERNAME%%:F"'
        )
    except OSError as 异常:  # 只读文件系统、权限不足……
        日志.warning("无法把生成的令牌写入 %s：%s", 环境文件, 异常)
    return 令牌


def 加载配置(环境文件: Path = 默认配置文件) -> 配置:
    """构建配置；令牌缺失时创建令牌文件。"""
    配置对象 = 配置(_env_file=环境文件)
    配置对象.auth_token = 确保令牌(环境文件, 配置对象)
    return 配置对象
