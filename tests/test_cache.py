from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from solver.cache import SQLite缓存


def test_写入后读出(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db", 有效秒数=1800)
    try:
        写入结果 = 缓存.写入("example.com", "cookie-value", "UA/1.0", 有效期=600)
        读出 = 缓存.查询("example.com")
        assert 读出 is not None
        assert 读出.凭据 == "cookie-value"
        assert 读出.用户代理 == "UA/1.0"
        assert 读出.创建时间 == pytest.approx(写入结果.创建时间)
        assert 读出.过期时间 == pytest.approx(写入结果.过期时间)
        assert 读出.剩余() <= 600
    finally:
        缓存.关闭()


def test_过期记录未命中并被删除(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db", 有效秒数=1800)
    try:
        缓存.写入("example.com", "v", "ua", 有效期=10, 当前时刻=1000.0)
        assert 缓存.查询("example.com", 当前时刻=1009.0) is not None
        assert 缓存.查询("example.com", 当前时刻=1010.0) is None
        assert 缓存.查询("example.com", 当前时刻=1005.0) is None  # 行已被删除
        assert 缓存.计数(当前时刻=1005.0) == 0
    finally:
        缓存.关闭()


def test_有效期被压到缓存上限(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db", 有效秒数=1800)
    try:
        记录 = 缓存.写入("example.com", "v", "ua", 有效期=99999, 当前时刻=0.0)
        assert 记录.过期时间 == pytest.approx(1800.0)
    finally:
        缓存.关闭()


def test_写入覆盖同一域名(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db")
    try:
        缓存.写入("example.com", "old", "ua-old")
        缓存.写入("example.com", "new", "ua-new")
        读出 = 缓存.查询("example.com")
        assert 读出 is not None
        assert (读出.凭据, 读出.用户代理) == ("new", "ua-new")
        assert 缓存.计数() == 1
    finally:
        缓存.关闭()


def test_域名相互隔离(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db")
    try:
        缓存.写入("a.example.com", "a", "ua")
        缓存.写入("b.example.com", "b", "ua")
        assert 缓存.查询("a.example.com").凭据 == "a"
        assert 缓存.查询("b.example.com").凭据 == "b"
        assert 缓存.查询("example.com") is None
    finally:
        缓存.关闭()


def test_删除记录(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db")
    try:
        缓存.写入("example.com", "v", "ua")
        assert 缓存.删除("example.com") is True
        assert 缓存.删除("example.com") is False
        assert 缓存.查询("example.com") is None
    finally:
        缓存.关闭()


def test_清理过期记录(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db")
    try:
        缓存.写入("old.example.com", "v", "ua", 有效期=5, 当前时刻=100.0)
        缓存.写入("new.example.com", "v", "ua", 有效期=500, 当前时刻=100.0)
        assert 缓存.清理过期(当前时刻=200.0) == 1
        assert 缓存.查询("new.example.com", 当前时刻=200.0) is not None
    finally:
        缓存.关闭()


def test_启用WAL模式(tmp_path):
    路径 = tmp_path / "c.db"
    缓存 = SQLite缓存(路径)
    缓存.关闭()
    连接 = sqlite3.connect(路径)
    try:
        assert 连接.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        连接.close()


def test_并发读写(tmp_path):
    缓存 = SQLite缓存(tmp_path / "c.db")
    栅栏 = threading.Barrier(8)
    错误列表: list[BaseException] = []

    def 工作线程(序号: int) -> None:
        try:
            栅栏.wait(timeout=5)
            for 轮次 in range(25):
                域名 = f"host{序号}.example.com"
                缓存.写入(域名, f"v{轮次}", f"ua{序号}")
                读出 = 缓存.查询(域名)
                assert 读出 is not None
            assert 缓存.查询(f"host{序号}.example.com") is not None
        except BaseException as 异常:
            错误列表.append(异常)

    try:
        with ThreadPoolExecutor(max_workers=8) as 线程池:
            list(线程池.map(工作线程, range(8)))
        assert 错误列表 == []
        assert 缓存.计数(当前时刻=time.time() + 1) == 8
    finally:
        缓存.关闭()
