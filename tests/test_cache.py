from __future__ import annotations

import time
from pathlib import Path

import pytest

import repo2resume.storage.cache as cache_mod
from repo2resume.storage.cache import SqliteCache, open_cache


def test_sqlite_cache_get_set_delete(tmp_path: Path) -> None:
    c = SqliteCache(tmp_path / "cache.db")
    assert c.get("k") is None
    c.set("k", "hello", ttl=60)
    assert c.get("k") == "hello"
    c.delete("k")
    assert c.get("k") is None
    c.close()


def test_sqlite_cache_ttl_expiry(tmp_path: Path) -> None:
    c = SqliteCache(tmp_path / "cache.db")
    c.set("soon", "v", ttl=1)
    assert c.get("soon") == "v"
    time.sleep(1.1)
    assert c.get("soon") is None
    c.close()


def test_sqlite_skips_oversized_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod, "MAX_VALUE_BYTES", 16)
    c = SqliteCache(tmp_path / "cache.db")
    c.set("big", "x" * 64)
    assert c.get("big") is None
    c.close()


def test_sqlite_evicts_when_over_payload_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_mod, "MAX_DB_BYTES", 80)
    monkeypatch.setattr(cache_mod, "MAX_VALUE_BYTES", 10_000)
    c = SqliteCache(tmp_path / "cache.db")
    for i in range(10):
        c.set(f"k{i}", "v" * 40, ttl=3600)
    assert c._payload_bytes() <= cache_mod.MAX_DB_BYTES
    assert c.get("k9") == "v" * 40
    c.close()


def test_open_cache_falls_back_when_redis_down(tmp_path: Path) -> None:
    cache = open_cache("redis://127.0.0.1:1/0", tmp_path / "cache.db")
    assert isinstance(cache, SqliteCache)
    cache.set("ok", "1", ttl=10)
    assert cache.get("ok") == "1"
    cache.close()
