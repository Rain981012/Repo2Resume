"""【手写】Phase 2.5 Step 5：ErrorRecoveryHook 错误恢复。

实现 agent/hooks.py 的 空 13（ErrorRecoveryHook.after）后，本文件应变绿。
核心契约：工具异常被吞成文本回填 LLM；PermissionDenied 不吞，照常抛出。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from repo2resume.agent.hooks import ErrorRecoveryHook, PermissionDenied
from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


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


def _make_deny_tool() -> Tool:
    """模拟一个在 handler 里直接抛 PermissionDenied 的工具
    （真实场景里 PermissionDenied 由 PermissionHook.before 抛，这里直接抛以测试 after 守卫）。
    """

    class _NoParams(BaseModel):
        pass

    def _handler() -> str:
        raise PermissionDenied("用户拒绝")

    return Tool(
        name="deny",
        description="raises PermissionDenied",
        params_model=_NoParams,
        handler=_handler,
        risk="readonly",
    )


def test_error_recovery_turns_exception_into_text() -> None:
    """工具抛 RuntimeError → call 不抛，返回含工具名和错误信息的文本。"""
    reg = ToolRegistry()
    reg.register(_make_boom_tool())
    reg.add_hook(ErrorRecoveryHook())

    out = reg.call("boom", {"msg": "数据库连不上"})
    assert isinstance(out, str)
    assert "boom" in out
    assert "数据库连不上" in out
    assert "RuntimeError" in out


def test_error_recovery_passes_through_success() -> None:
    """成功路径 → 原样返回 result，不被 hook 改成文本。"""
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(ErrorRecoveryHook())

    assert reg.call("add", {"a": 2, "b": 3}) == 5


def test_error_recovery_does_not_swallow_permission_denied() -> None:
    """PermissionDenied 不吞 → 照常抛出，不变成文本。"""
    reg = ToolRegistry()
    reg.register(_make_deny_tool())
    reg.add_hook(ErrorRecoveryHook())

    with pytest.raises(PermissionDenied):
        reg.call("deny", {})


def test_error_recovery_text_format() -> None:
    """文本格式包含 [工具 ... 执行失败: ...] 标记，方便 LLM 识别这是错误回填。"""
    reg = ToolRegistry()
    reg.register(_make_boom_tool())
    reg.add_hook(ErrorRecoveryHook())

    out = reg.call("boom", {"msg": "x"})
    assert out.startswith("[工具 boom 执行失败:")
    assert "请修正参数或换种方式后重试" in out
