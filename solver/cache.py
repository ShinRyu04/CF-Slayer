"""Per-domain SQLite cache for clearance credentials (WAL, TTL, thread-safe)."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clearances (
    domain      TEXT PRIMARY KEY,
    clearance   TEXT NOT NULL,
    user_agent  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class CachedClearance:
    domain: str
    clearance: str
    user_agent: str
    created_at: float
    expires_at: float

    def remaining(self, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (time.time() if now is None else now)))


class SQLiteCache:
    """One connection guarded by a re-entrant lock; safe across FastAPI worker threads.

    A single connection is deliberate: the service is one process, writes are rare
    (once per solve), and reads are sub-millisecond. WAL only helps external readers,
    but it also prevents a long read from blocking the write lock.
    """

    def __init__(self, path: str | Path, ttl_seconds: int = 1800) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=10.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(_SCHEMA)

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def get(self, domain: str, now: float | None = None) -> CachedClearance | None:
        """Return a live entry; expired rows are deleted and reported as a miss."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT domain, clearance, user_agent, created_at, expires_at "
                "FROM clearances WHERE domain = ?",
                (domain,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                self._conn.execute("DELETE FROM clearances WHERE domain = ?", (domain,))
                return None
            return CachedClearance(
                domain=row["domain"],
                clearance=row["clearance"],
                user_agent=row["user_agent"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )

    def put(
        self,
        domain: str,
        clearance: str,
        user_agent: str,
        ttl: int | None = None,
        now: float | None = None,
    ) -> CachedClearance:
        """Upsert a credential. ``ttl`` is clamped to the configured maximum."""
        now = time.time() if now is None else now
        effective = min(ttl, self._ttl) if ttl is not None else self._ttl
        if effective <= 0:
            raise ValueError("ttl must be positive")
        expires_at = now + effective
        with self._lock:
            self._conn.execute(
                "INSERT INTO clearances (domain, clearance, user_agent, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET "
                "clearance = excluded.clearance, "
                "user_agent = excluded.user_agent, "
                "created_at = excluded.created_at, "
                "expires_at = excluded.expires_at",
                (domain, clearance, user_agent, now, expires_at),
            )
        return CachedClearance(domain, clearance, user_agent, now, expires_at)

    def invalidate(self, domain: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM clearances WHERE domain = ?", (domain,))
            return cur.rowcount > 0

    def purge_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute("DELETE FROM clearances WHERE expires_at <= ?", (now,))
            return cur.rowcount

    def count(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM clearances WHERE expires_at > ?", (now,)
            ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
