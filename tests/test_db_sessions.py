"""【AI 生成】chat_sessions 持久化单测。"""

from __future__ import annotations

from repo2resume.storage.db import Database


def _db(tmp_path):
    return Database(tmp_path / "test.db")


def test_save_and_load_session(tmp_path) -> None:
    db = _db(tmp_path)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    db.save_session("s1", msgs, title="hi")
    loaded = db.load_session("s1")
    assert loaded == msgs
    db.close()


def test_load_missing_session_returns_none(tmp_path) -> None:
    db = _db(tmp_path)
    assert db.load_session("nope") is None
    db.close()


def test_save_updates_existing_session(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("s1", [{"role": "user", "content": "a"}])
    db.save_session("s1", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert len(db.load_session("s1")) == 2
    db.close()


def test_list_sessions_orders_by_updated(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("old", [{"role": "user", "content": "x"}])
    db.save_session("new", [{"role": "user", "content": "y"}])
    sessions = db.list_sessions()
    assert sessions[0]["id"] == "new"
    assert sessions[1]["id"] == "old"
    db.close()
