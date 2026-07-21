"""SQLite persistence for profiles, drafts, sessions, and traces."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS skill_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        label TEXT,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS resume_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        job_id TEXT,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        title TEXT,
        messages_json TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS llm_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_id TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        latency_ms INTEGER,
        payload_json TEXT
    );
    """,
}


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def current_version(self) -> int:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        ver = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(ver[0]) if ver else 0

    def migrate(self) -> None:
        current = self.current_version()
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            self._conn.executescript(MIGRATIONS[version])
            self._conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- chat sessions -------------------------------------------------

    def save_session(
        self, session_id: str, messages: list[dict], title: str | None = None
    ) -> None:
        """Upsert a chat session's messages (and optionally title)."""
        import json

        payload = json.dumps(messages, ensure_ascii=False)
        existing = self._conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO chat_sessions(id, title, messages_json) VALUES (?, ?, ?)",
                (session_id, title, payload),
            )
        else:
            if title is not None:
                self._conn.execute(
                    "UPDATE chat_sessions SET messages_json = ?, title = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (payload, title, session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE chat_sessions SET messages_json = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (payload, session_id),
                )
        self._conn.commit()

    def load_session(self, session_id: str) -> list[dict] | None:
        """Return messages for a session, or None if it doesn't exist."""
        import json

        row = self._conn.execute(
            "SELECT messages_json FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["messages_json"])

    def list_sessions(self, limit: int = 10) -> list[dict]:
        """Most-recently-updated sessions (rowid 作 tiebreaker，避免同秒打平)."""
        rows = self._conn.execute(
            "SELECT id, title, updated_at FROM chat_sessions "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]



def open_db(path: Path) -> Database:
    return Database(path)
