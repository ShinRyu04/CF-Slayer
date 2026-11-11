from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from solver.cache import SQLiteCache


def test_put_get_roundtrip(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db", ttl_seconds=1800)
    try:
        put = cache.put("example.com", "cookie-value", "UA/1.0", ttl=600)
        got = cache.get("example.com")
        assert got is not None
        assert got.clearance == "cookie-value"
        assert got.user_agent == "UA/1.0"
        assert got.created_at == pytest.approx(put.created_at)
        assert got.expires_at == pytest.approx(put.expires_at)
        assert got.remaining() <= 600
    finally:
        cache.close()


def test_expired_entry_is_a_miss_and_is_deleted(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db", ttl_seconds=1800)
    try:
        cache.put("example.com", "v", "ua", ttl=10, now=1000.0)
        assert cache.get("example.com", now=1009.0) is not None
        assert cache.get("example.com", now=1010.0) is None
        assert cache.get("example.com", now=1005.0) is None  # row was deleted
        assert cache.count(now=1005.0) == 0
    finally:
        cache.close()


def test_ttl_is_clamped_to_cache_maximum(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db", ttl_seconds=1800)
    try:
        entry = cache.put("example.com", "v", "ua", ttl=99999, now=0.0)
        assert entry.expires_at == pytest.approx(1800.0)
    finally:
        cache.close()


def test_put_overwrites_existing_domain(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db")
    try:
        cache.put("example.com", "old", "ua-old")
        cache.put("example.com", "new", "ua-new")
        got = cache.get("example.com")
        assert got is not None
        assert (got.clearance, got.user_agent) == ("new", "ua-new")
        assert cache.count() == 1
    finally:
        cache.close()


def test_domains_are_isolated(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db")
    try:
        cache.put("a.example.com", "a", "ua")
        cache.put("b.example.com", "b", "ua")
        assert cache.get("a.example.com").clearance == "a"
        assert cache.get("b.example.com").clearance == "b"
        assert cache.get("example.com") is None
    finally:
        cache.close()


def test_invalidate(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db")
    try:
        cache.put("example.com", "v", "ua")
        assert cache.invalidate("example.com") is True
        assert cache.invalidate("example.com") is False
        assert cache.get("example.com") is None
    finally:
        cache.close()


def test_purge_expired(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db")
    try:
        cache.put("old.example.com", "v", "ua", ttl=5, now=100.0)
        cache.put("new.example.com", "v", "ua", ttl=500, now=100.0)
        assert cache.purge_expired(now=200.0) == 1
        assert cache.get("new.example.com", now=200.0) is not None
    finally:
        cache.close()


def test_wal_mode_is_enabled(tmp_path):
    path = tmp_path / "c.db"
    cache = SQLiteCache(path)
    cache.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_concurrent_reads_and_writes(tmp_path):
    cache = SQLiteCache(tmp_path / "c.db")
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for round_no in range(25):
                domain = f"host{index}.example.com"
                cache.put(domain, f"v{round_no}", f"ua{index}")
                got = cache.get(domain)
                assert got is not None
            assert cache.get(f"host{index}.example.com") is not None
        except BaseException as exc:
            errors.append(exc)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        assert errors == []
        assert cache.count(now=time.time() + 1) == 8
    finally:
        cache.close()
