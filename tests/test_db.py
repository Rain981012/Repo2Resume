from __future__ import annotations

from pathlib import Path

from repo2resume.storage.db import SCHEMA_VERSION, open_db


def test_open_db_migrates(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    assert db.current_version() == SCHEMA_VERSION
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "skill_profiles" in tables
    assert "chat_sessions" in tables
    assert "llm_traces" in tables
    db.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "repo2resume.db"
    db1 = open_db(path)
    db1.close()
    db2 = open_db(path)
    assert db2.current_version() == SCHEMA_VERSION
    db2.close()
