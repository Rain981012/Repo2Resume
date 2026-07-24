"""【手写】Phase 2.5 Step 1 / Step 2：ToolRegistry 的 lifecycle hooks + error 语义。

实现 agent/tools.py 的 空 7（add_hook）、空 8/9（call 跑 hooks + 处理异常）后，本文件应变绿。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


class _DoubleArgsAddThousandHook:
    """before: 每个参数翻倍；after: result + 1000。"""

    def before(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return {k: v * 2 for k, v in arguments.items()}

    def after(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        error: BaseException | None,
    ) -> Any:
        return result + 1000


class _NoOpHook:
    """before 返回 None（不动参数）；after 原样返回 result。"""

    def before(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def after(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        error: BaseException | None,
    ) -> Any:
        return result


def test_hook_rewrites_args_and_result() -> None:
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(_DoubleArgsAddThousandHook())
    # add(2,3) → before 翻倍成 (4,6) → handler 4+6=10 → after +1000 = 1010
    assert reg.call("add", {"a": 2, "b": 3}) == 1010


def test_no_hook_keeps_old_behavior() -> None:
    """没注册 hook 时 call 行为不变（Phase 2 老测试也要继续绿）。"""
    reg = ToolRegistry()
    reg.register(make_add_tool())
    assert reg.call("add", {"a": 10, "b": 1}) == 11


def test_noop_hook_keeps_result() -> None:
    """before 返回 None、after 原样返回时，结果与无 hook 一致。"""
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(_NoOpHook())
    assert reg.call("add", {"a": 5, "b": 7}) == 12


def test_multiple_hooks_run_in_order() -> None:
    """多个 hook 按注册顺序串行：前一个的输出是后一个的输入。"""

    class _PlusOneBefore:
        def before(self, name, arguments):
            return {k: v + 1 for k, v in arguments.items()}

        def after(self, name, arguments, result, error):
            return result

    class _TimesTenAfter:
        def before(self, name, arguments):
            return None

        def after(self, name, arguments, result, error):
            return result * 10

    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(_PlusOneBefore())  # before: (2,3)→(3,4)
    reg.add_hook(_TimesTenAfter())  # after: 7→70
    assert reg.call("add", {"a": 2, "b": 3}) == 70


# ===========================================================================
# Step 2：error 语义——after 能见到异常，并允许吞掉
# ===========================================================================


class _BoomParams(BaseModel):
    pass


def _boom_handler() -> None:
    raise RuntimeError("boom")


def _boom_tool() -> Tool:
    """一个永远抛 RuntimeError 的工具，用来测 error 路径。"""
    return Tool(
        name="boom",
        description="always raises RuntimeError",
        params_model=_BoomParams,
        handler=_boom_handler,
    )


class _RecoverHook:
    """after 见到 error 时返回恢复文本（非 None）→ 吞掉异常。"""

    def before(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def after(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        error: BaseException | None,
    ) -> Any:
        assert error is not None, "after 应该能见到 error"
        return f"recovered: {error}"


def test_tool_error_propagates_without_hook() -> None:
    """没注册 hook 时，工具异常原样抛出（回归保护）。"""
    reg = ToolRegistry()
    reg.register(_boom_tool())
    with pytest.raises(RuntimeError, match="boom"):
        reg.call("boom", {})


def test_after_hook_can_swallow_error() -> None:
    """after 返回非 None → 吞掉异常，call 返回恢复文本，不抛。"""
    reg = ToolRegistry()
    reg.register(_boom_tool())
    reg.add_hook(_RecoverHook())
    out = reg.call("boom", {})
    assert out == "recovered: boom"


def test_after_hook_returning_result_keeps_error() -> None:
    """after 返回 result（错误路径下 result=None）→ 不吞，异常仍抛出。

    _NoOpHook.after 返回 result；错误路径下 result 是 None，
    按契约「error 非空 + after 返回 None → 不吞」→ 异常保留。
    """
    reg = ToolRegistry()
    reg.register(_boom_tool())
    reg.add_hook(_NoOpHook())
    with pytest.raises(RuntimeError, match="boom"):
        reg.call("boom", {})


def test_success_path_still_works_after_step2() -> None:
    """加了 error 语义后，成功路径行为不变（Step 1 测试的回归保护）。"""
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.add_hook(_DoubleArgsAddThousandHook())
    assert reg.call("add", {"a": 2, "b": 3}) == 1010
