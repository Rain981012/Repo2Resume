"""【手写】Phase 2.5 Step 4：TraceHook 观测 + db.record_tool_trace / tool_trace_stats。

实现 agent/hooks.py 的 空 11/空 12 后，本文件应变绿。
db 方法（record_tool_trace / tool_trace_stats）已由脚手架提供。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from repo2resume.agent.hooks import TraceHook
from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool
from repo2resume.storage.db import open_db


class _BoomParams(BaseModel):
    msg: str = "boom"


def _make_boom_tool() -> Tool:
    def _handler(msg: str = "boom") -> str:
        raise RuntimeError(msg)

    return Tool(
        name="boom",
        description="always raises",
        params_model=_BoomParams,
        handler=_handler,
        risk="readonly",
    )


def _new_db(tmp_path):
    return open_db(tmp_path / "trace.db")


def test_trace_records_successful_call(tmp_path) -> None:
    """成功调用 → tool_traces 写一行，result_json 有值、error 为 NULL。"""
    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(TraceHook(db, session_id="s1"))

    assert reg.call("add", {"a": 2, "b": 3}) == 5

    rows = db.conn.execute(
        "SELECT tool_name, error, result_json, latency_ms FROM tool_traces WHERE session_id='s1'"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["tool_name"] == "add"
    assert r["error"] is None
    assert r["result_json"] == "5"
    assert r["latency_ms"] >= 0


def test_trace_records_failed_call(tmp_path) -> None:
    """失败调用 → error 有值、result_json 为 NULL；异常仍照常抛出。"""
    import pytest

    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(_make_boom_tool())
    reg.add_hook(TraceHook(db, session_id="s2"))

    with pytest.raises(RuntimeError):
        reg.call("boom", {"msg": "oops"})

    rows = db.conn.execute(
        "SELECT error, result_json FROM tool_traces WHERE session_id='s2'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["error"] is not None
    assert "oops" in rows[0]["error"]
    assert rows[0]["result_json"] is None


def test_tool_trace_stats_aggregates(tmp_path) -> None:
    """多次调用 → stats 聚合 count/total_ms/errors，per_tool 按工具分组。"""
    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.register(_make_boom_tool())
    reg.add_hook(TraceHook(db, session_id="s3"))

    reg.call("add", {"a": 1, "b": 1})  # ok
    reg.call("add", {"a": 2, "b": 2})  # ok
    try:
        reg.call("boom", {"msg": "x"})
    except RuntimeError:
        pass

    stats = db.tool_trace_stats("s3")
    assert stats["count"] == 3
    assert stats["errors"] == 1
    assert stats["total_ms"] >= 0
    assert stats["per_tool"]["add"]["count"] == 2
    assert stats["per_tool"]["add"]["errors"] == 0
    assert stats["per_tool"]["boom"]["count"] == 1
    assert stats["per_tool"]["boom"]["errors"] == 1


def test_trace_isolated_per_session(tmp_path) -> None:
    """不同 session 的 trace 互不干扰。"""
    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(TraceHook(db, session_id="a"))
    reg.add_hook(TraceHook(db, session_id="b"))

    reg.call("add", {"a": 1, "b": 1})
    # 注意：两个 hook 都会记，所以一次调用产生 2 行（session a + session b）
    stats_a = db.tool_trace_stats("a")
    stats_b = db.tool_trace_stats("b")
    assert stats_a["count"] == 1
    assert stats_b["count"] == 1


def test_trace_records_permission_denied(tmp_path) -> None:
    from repo2resume.agent.hooks import PermissionDenied, PermissionHook

    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: "write",
            needs_confirm_risks={"write"},
            confirm=lambda n, a: False,
        )
    )
    reg.add_hook(TraceHook(db, session_id="deny"))
    with pytest.raises(PermissionDenied):
        reg.call("add", {"a": 1, "b": 2})
    rows = db.conn.execute(
        "SELECT tool_name, error FROM tool_traces WHERE session_id='deny'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "add"
    assert "拒绝" in (rows[0][1] or "")


def test_tool_trace_stats_filters_by_run_id(tmp_path) -> None:
    import repo2resume.observability.run_context as rc

    db = _new_db(tmp_path)
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(TraceHook(db, session_id="s"))
    rc._turn_depth.set(0)
    rc.enter_agent_turn()
    rid = rc.current_run_id()
    reg.call("add", {"a": 1, "b": 1})
    rc.exit_agent_turn()
    assert db.tool_trace_stats("s", run_id=rid)["count"] == 1
    assert db.tool_trace_stats("s", run_id="turn-missing")["count"] == 0
