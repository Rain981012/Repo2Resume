"""【手写】Phase 2.5 Step 3：PermissionHook 权限分级。

实现 agent/hooks.py 的 空 10（PermissionHook.before）后，本文件应变绿。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from repo2resume.agent.hooks import PermissionDenied, PermissionHook
from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


class _WriteParams(BaseModel):
    path: str
    content: str = ""


def _make_write_tool(calls: dict) -> Tool:
    """模拟一个「写文件」工具，记录是否被调到。"""

    def _handler(path: str, content: str = "") -> str:
        calls.setdefault("ran", []).append((path, content))
        return "ok"

    return Tool(
        name="write_file",
        description="write a file (dangerous)",
        params_model=_WriteParams,
        handler=_handler,
        risk="write",
    )


def test_readonly_tool_allowed_without_confirm() -> None:
    """只读工具（不在 needs_confirm）→ 放行，confirm 不该被调。"""
    confirm_calls: list = []

    def confirm(name, args):
        confirm_calls.append((name, args))
        return True

    reg = ToolRegistry()
    reg.register(make_add_tool())  # add 是只读
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write"},
            confirm=confirm,
        )
    )

    assert reg.call("add", {"a": 2, "b": 3}) == 5
    assert confirm_calls == []  # 只读工具不该问


def test_write_tool_allowed_when_confirmed() -> None:
    """写工具 + 用户确认 True → 放行，handler 执行。"""
    calls: dict = {}

    def confirm(name, args):
        return True

    reg = ToolRegistry()
    reg.register(_make_write_tool(calls))
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write"},
            confirm=confirm,
        )
    )

    assert reg.call("write_file", {"path": "/tmp/x", "content": "hi"}) == "ok"
    assert calls["ran"] == [("/tmp/x", "hi")]


def test_write_tool_denied_raises() -> None:
    """写工具 + 用户确认 False → PermissionDenied，handler 不该执行。"""
    calls: dict = {}

    def confirm(name, args):
        return False

    reg = ToolRegistry()
    reg.register(_make_write_tool(calls))
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write"},
            confirm=confirm,
        )
    )

    with pytest.raises(PermissionDenied):
        reg.call("write_file", {"path": "/tmp/x"})
    assert "ran" not in calls  # handler 没被调


def test_confirm_receives_name_and_args() -> None:
    """confirm 能拿到工具名和参数，方便给用户看「要执行啥」。"""
    seen: list = []

    def confirm(name, args):
        seen.append((name, args))
        return True

    reg = ToolRegistry()
    reg.register(_make_write_tool({}))
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write"},
            confirm=confirm,
        )
    )

    reg.call("write_file", {"path": "/tmp/y", "content": "z"})
    assert seen == [("write_file", {"path": "/tmp/y", "content": "z"})]
