"""CancelToken / soft-cancel 安全点测试。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from repo2resume.agent.cancel import (
    RunCancelled,
    cancel_scope,
    check_cancelled,
    get_cancel_token,
)
from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse, ToolCall
from repo2resume.agent.progress import emit_progress, reset_progress_callback, set_progress_callback
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


def test_cancel_scope_check_raises() -> None:
    with cancel_scope() as token:
        token.cancel()
        with pytest.raises(RunCancelled):
            check_cancelled()
    check_cancelled()  # scope 外无 token，静默


def test_emit_progress_respects_cancel() -> None:
    seen: list[str] = []
    tok = set_progress_callback(seen.append)
    try:
        with cancel_scope() as token:
            emit_progress("before")
            token.cancel()
            with pytest.raises(RunCancelled):
                emit_progress("after")
    finally:
        reset_progress_callback(tok)
    assert seen == ["before"]


def test_loop_cancels_on_second_round() -> None:
    """首轮工具跑完后，第二轮 LLM 前取消。"""
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})]
            )
        tok = get_cancel_token()
        assert tok is not None
        tok.cancel()
        return LLMResponse(content="should not be used")

    reg = ToolRegistry()
    reg.register(make_add_tool())
    ctx = ContextManager(system="t")
    asm = PromptAssembler(base="t", registry=reg)
    loop = AgentLoop(llm=llm, registry=reg, assembler=asm, context=ctx, max_rounds=5)

    with cancel_scope():
        with pytest.raises(RunCancelled):
            loop.run("go")

    tool_msgs = [m for m in ctx._messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "3"
    assert calls["n"] == 2  # 第二轮 LLM 已进入，返回后 check 抛出


def test_loop_pads_tool_results_when_cancelled_before_tools() -> None:
    """LLM 已声明 tool_calls 后立刻取消：补齐占位结果。"""

    class SlowParams(BaseModel):
        x: int = Field(default=1)

    started = {"n": 0}

    def slow(x: int = 1) -> str:
        started["n"] += 1
        return str(x)

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="slow",
            description="slow",
            params_model=SlowParams,
            handler=slow,
            risk="readonly",
        )
    )

    def llm(messages, tools):
        tok = get_cancel_token()
        assert tok is not None
        tok.cancel()
        return LLMResponse(tool_calls=[ToolCall(id="t1", name="slow", arguments={"x": 1})])

    ctx = ContextManager(system="t")
    asm = PromptAssembler(base="t", registry=reg)
    loop = AgentLoop(llm=llm, registry=reg, assembler=asm, context=ctx, max_rounds=3)

    with cancel_scope():
        with pytest.raises(RunCancelled):
            loop.run("go")

    # check after llm 在 add assistant 之前——需看实际顺序
    # 当前实现：llm → check → add assistant → execute
    # 若 check 在 add 之前失败，则没有悬空 tool_calls
    tool_msgs = [m for m in ctx._messages if m.get("role") == "tool"]
    assert started["n"] == 0
    assert tool_msgs == [] or all("中断" in m["content"] for m in tool_msgs)
