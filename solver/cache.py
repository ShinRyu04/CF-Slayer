"""按域名缓存放行凭据的 SQLite 缓存（WAL、TTL、线程安全）。"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_建表语句 = """
CREATE TABLE IF NOT EXISTS clearances (
    domain      TEXT PRIMARY KEY,
    clearance   TEXT NOT NULL,
    user_agent  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class 缓存凭据:
    域名: str
    凭据: str
    用户代理: str
    创建时间: float
    过期时间: float

    def 剩余(self, 当前时刻: float | None = None) -> int:
        return max(0, int(self.过期时间 - (time.time() if 当前时刻 is None else 当前时刻)))


class SQLite缓存:
    """单连接 + 可重入锁；可跨 FastAPI 工作线程安全使用。

    只用一个连接是有意为之：服务是单进程，写入很少（每次求解一次），
    读取在亚毫秒级。WAL 主要利好外部读者，但也避免长时间读操作卡住写锁。
    """

    def __init__(self, 路径: str | Path, 有效秒数: int = 1800) -> None:
        if 有效秒数 <= 0:
            raise ValueError("有效秒数必须为正数")
        self._路径 = Path(路径)
        if str(self._路径) != ":memory:":
            self._路径.parent.mkdir(parents=True, exist_ok=True)
        self._有效期 = 有效秒数
        self._锁 = threading.RLock()
        self._连接 = sqlite3.connect(
            str(self._路径), check_same_thread=False, timeout=10.0, isolation_level=None
        )
        self._连接.row_factory = sqlite3.Row
        with self._锁:
            self._连接.execute("PRAGMA journal_mode=WAL")
            self._连接.execute("PRAGMA synchronous=NORMAL")
            self._连接.execute("PRAGMA busy_timeout=5000")
            self._连接.execute(_建表语句)

    @property
    def 有效秒数(self) -> int:
        return self._有效期

    def 查询(self, 域名: str, 当前时刻: float | None = None) -> 缓存凭据 | None:
        """返回仍然有效的记录；过期行会被删除并按未命中处理。"""
        当前时刻 = time.time() if 当前时刻 is None else 当前时刻
        with self._锁:
            记录行 = self._连接.execute(
                "SELECT domain, clearance, user_agent, created_at, expires_at "
                "FROM clearances WHERE domain = ?",
                (域名,),
            ).fetchone()
            if 记录行 is None:
                return None
            if 记录行["expires_at"] <= 当前时刻:
                self._连接.execute("DELETE FROM clearances WHERE domain = ?", (域名,))
                return None
            return 缓存凭据(
                域名=记录行["domain"],
                凭据=记录行["clearance"],
                用户代理=记录行["user_agent"],
                创建时间=记录行["created_at"],
                过期时间=记录行["expires_at"],
            )

    def 写入(
        self,
        域名: str,
        凭据: str,
        用户代理: str,
        有效期: int | None = None,
        当前时刻: float | None = None,
    ) -> 缓存凭据:
        """写入或更新凭据。``有效期`` 会被压到配置上限以内。"""
        当前时刻 = time.time() if 当前时刻 is None else 当前时刻
        实际有效期 = min(有效期, self._有效期) if 有效期 is not None else self._有效期
        if 实际有效期 <= 0:
            raise ValueError("有效期必须为正数")
        过期时间 = 当前时刻 + 实际有效期
        with self._锁:
            self._连接.execute(
                "INSERT INTO clearances (domain, clearance, user_agent, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET "
                "clearance = excluded.clearance, "
                "user_agent = excluded.user_agent, "
                "created_at = excluded.created_at, "
                "expires_at = excluded.expires_at",
                (域名, 凭据, 用户代理, 当前时刻, 过期时间),
            )
        return 缓存凭据(域名, 凭据, 用户代理, 当前时刻, 过期时间)

    def 删除(self, 域名: str) -> bool:
        with self._锁:
            游标 = self._连接.execute("DELETE FROM clearances WHERE domain = ?", (域名,))
            return 游标.rowcount > 0

    def 清理过期(self, 当前时刻: float | None = None) -> int:
        当前时刻 = time.time() if 当前时刻 is None else 当前时刻
        with self._锁:
            游标 = self._连接.execute(
                "DELETE FROM clearances WHERE expires_at <= ?", (当前时刻,)
            )
            return 游标.rowcount

    def 计数(self, 当前时刻: float | None = None) -> int:
        当前时刻 = time.time() if 当前时刻 is None else 当前时刻
        with self._锁:
            记录行 = self._连接.execute(
                "SELECT COUNT(*) AS n FROM clearances WHERE expires_at > ?", (当前时刻,)
            ).fetchone()
        return int(记录行["n"])

    def 关闭(self) -> None:
        with self._锁:
            self._连接.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *异常信息: object) -> None:
        self.关闭()
