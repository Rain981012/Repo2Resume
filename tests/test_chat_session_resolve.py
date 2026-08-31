"""默认续最近一次 chat session。"""

from __future__ import annotations

import pytest

from repo2resume.chat_ui.repl import resolve_chat_session
from repo2resume.storage.db import Database


def _db(tmp_path):
    return Database(tmp_path / "test.db")


def test_empty_db_starts_new_session(tmp_path) -> None:
    db = _db(tmp_path)
    ref = resolve_chat_session(db)
    assert ref.kind == "new"
    assert ref.history is None
    assert len(ref.session_id) == 12
    db.close()


def test_plain_chat_continues_latest_session(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("old", [{"role": "user", "content": "a"}])
    db.save_session("new", [{"role": "user", "content": "b"}])
    ref = resolve_chat_session(db)
    assert ref.kind == "continued"
    assert ref.session_id == "new"
    assert ref.history == [{"role": "user", "content": "b"}]
    db.close()


def test_new_flag_ignores_latest(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("keep", [{"role": "user", "content": "x"}])
    ref = resolve_chat_session(db, new=True)
    assert ref.kind == "new"
    assert ref.session_id != "keep"
    assert ref.history is None
    db.close()


def test_explicit_resume(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("s1", [{"role": "user", "content": "hi"}])
    db.save_session("s2", [{"role": "user", "content": "later"}])
    ref = resolve_chat_session(db, resume="s1")
    assert ref.kind == "explicit"
    assert ref.session_id == "s1"
    db.close()


def test_resume_missing_raises(tmp_path) -> None:
    db = _db(tmp_path)
    with pytest.raises(LookupError):
        resolve_chat_session(db, resume="nope")
    db.close()


def test_resume_and_new_conflict(tmp_path) -> None:
    db = _db(tmp_path)
    db.save_session("s1", [{"role": "user", "content": "x"}])
    with pytest.raises(ValueError, match="不能同时"):
        resolve_chat_session(db, resume="s1", new=True)
    db.close()
