"""缓存后端抽象 + 两个实现：Redis（首选）/ SQLite（兜底），带 TTL 与容量上限。

`CacheBackend` 是 Protocol（get/set/delete/clear/close），`RedisCache` 和 `SqliteCache`
各自实现。单值超 512KB 跳过写入；SQLite 库超 100MB 按 created_at FIFO 淘汰。`open_cache`
优先 Redis，连不上自动降级 SQLite，对调用方透明。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

MAX_VALUE_BYTES = 512 * 1024
MAX_DB_BYTES = 100 * 1024 * 1024


class CacheBackend(Protocol):
    """缓存后端接口：get/set/delete/clear/close，Redis 与 SQLite 共用同一契约。"""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


class RedisCache:
    """基于 redis-py 的缓存实现，支持 TTL（`setex`）与单值大小上限。"""

    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._client.ping()

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            logger.warning("cache skip: value for %s exceeds %s bytes", key, MAX_VALUE_BYTES)
            return
        if ttl is None:
            self._client.set(key, value)
        else:
            self._client.setex(key, ttl, value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def clear(self) -> None:
        self._client.flushdb()

    def close(self) -> None:
        self._client.close()


class SqliteCache:
    """SQLite 缓存：建 `cache` 表存 key/value/expires_at/created_at，支持 TTL 与 FIFO 容量淘汰。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
        self._conn.commit()
        self.purge_expired()

    def get(self, key: str) -> str | None:
        self.purge_expired()
        row = self._conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at <= time.time():
            self.delete(key)
            return None
        return value

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            logger.warning("cache skip: value for %s exceeds %s bytes", key, MAX_VALUE_BYTES)
            return
        now = time.time()
        expires_at = (now + ttl) if ttl is not None else None
        self._conn.execute(
            """
            INSERT INTO cache(key, value, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                expires_at = excluded.expires_at,
                created_at = excluded.created_at
            """,
            (key, value, expires_at, now),
        )
        self._conn.commit()
        self._enforce_size_cap()

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        self._conn.commit()

    def clear(self) -> None:
        self._conn.execute("DELETE FROM cache")
        self._conn.commit()

    def purge_expired(self) -> None:
        self._conn.execute(
            "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (time.time(),),
        )
        self._conn.commit()

    def _payload_bytes(self) -> int:
        row = self._conn.execute("SELECT COALESCE(SUM(LENGTH(value)), 0) FROM cache").fetchone()
        return int(row[0]) if row else 0

    def _enforce_size_cap(self) -> None:
        """库超 100MB 时按 created_at 升序 FIFO 淘汰最旧记录，直到回到上限以下再 VACUUM。"""
        if self._payload_bytes() <= MAX_DB_BYTES:
            return
        while self._payload_bytes() > MAX_DB_BYTES:
            rows = self._conn.execute(
                "SELECT key FROM cache ORDER BY created_at ASC LIMIT 50"
            ).fetchall()
            if not rows:
                break
            self._conn.executemany(
                "DELETE FROM cache WHERE key = ?",
                [(r[0],) for r in rows],
            )
            self._conn.commit()
        self._conn.execute("VACUUM")

    def close(self) -> None:
        self._conn.close()


def open_cache(redis_url: str, sqlite_path: Path) -> CacheBackend:
    """工厂：优先 Redis，连不上自动降级到 SQLite 缓存，调用方无感。"""
    try:
        cache: CacheBackend = RedisCache(redis_url)
        logger.info("cache backend: redis (%s)", redis_url)
        return cache
    except Exception as exc:  # noqa: BLE001 — intentional degrade path
        logger.warning(
            "Redis unavailable (%s); falling back to SQLite cache at %s",
            exc,
            sqlite_path,
        )
        return SqliteCache(sqlite_path)
