"""【手写】TDD：实现 agent/loop.py 各空后，测试应变绿。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse, ToolCall
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


def _make_loop(llm):
    reg = ToolRegistry()
    reg.register(make_add_tool())
    ctx = ContextManager(system="你是助手")
    asm = PromptAssembler(base="你是助手", registry=reg)
    return AgentLoop(llm=llm, registry=reg, assembler=asm, context=ctx, max_rounds=5)


def test_direct_reply_no_tool() -> None:
    """LLM 直接给文本 → 立即返回，不调工具。"""

    def llm(messages, tools):
        return LLMResponse(content="你好")

    loop = _make_loop(llm)
    assert loop.run("hi") == "你好"


def test_single_tool_then_reply() -> None:
    """LLM 先调 add(2,3)，下一轮回复结果。"""
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
            )
        # 第二轮应能看到 tool 结果 "5"
        last = messages[-1]
        assert last["role"] == "tool"
        assert last["content"] == "5"
        return LLMResponse(content="结果是 5")

    loop = _make_loop(llm)
    assert loop.run("算 2+3") == "结果是 5"
    assert calls["n"] == 2
    assert loop.last_stop_reason == "final_text"


def test_max_rounds_stops_infinite_tool_calls() -> None:
    """LLM 一直要调工具 → 达到 max_rounds 强制停。"""

    def llm(messages, tools):
        return LLMResponse(tool_calls=[ToolCall(id="c", name="add", arguments={"a": 1, "b": 1})])

    loop = _make_loop(llm)
    loop._max_repeated_tool = 99
    out = loop.run("无限调")
    assert "最大轮数" in out or "停止" in out
    assert loop.last_stop_reason == "max_rounds"


def test_tool_result_recorded_in_history() -> None:
    """工具结果要回填进历史，下一轮 LLM 能看到。"""
    seen = {}

    def llm(messages, tools):
        if "tool" in [m["role"] for m in messages]:
            seen["tool_msg"] = next(m for m in messages if m["role"] == "tool")
            return LLMResponse(content="done")
        return LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})])

    loop = _make_loop(llm)
    loop.run("go")
    assert seen["tool_msg"]["content"] == "3"
    assert seen["tool_msg"]["tool_call_id"] == "c1"


def test_on_event_fires_for_tool_calls() -> None:
    """on_event 回调应在 LLM 响应、工具开始/结束时触发，供 CLI 显示 spinner 状态。"""
    events: list[tuple[str, ...]] = []

    def llm(messages, tools):
        if "tool" in [m["role"] for m in messages]:
            return LLMResponse(content="done")
        return LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})])

    def on_event(kind, data):
        events.append((kind, data))

    loop = _make_loop(llm)
    loop.run("go", on_event=on_event)

    # 期望事件：llm_response(含 tool_calls)、tool_start、tool_done、最终 llm_response
    kinds = [e[0] for e in events]
    assert "llm_response" in kinds
    assert "tool_start" in kinds
    assert "tool_done" in kinds
    # tool_start 的 data 应该是工具名 "add"
    tool_start = next(e for e in events if e[0] == "tool_start")
    assert tool_start[1] == "add"


def test_early_return_marker_skips_further_llm_rounds() -> None:
    """工具结果含 early_return_markers 时直接返回，不再问 LLM（防搜岗后空转）。"""
    calls = {"n": 0}

    class _Params(BaseModel):
        q: str = ""

    done_text = "【job_scout已完成】\n- [j1] Demo @ Co\n  链接：https://example.com"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="job_scout",
            description="scout",
            params_model=_Params,
            handler=lambda **_: done_text,
            risk="readonly",
        )
    )
    ctx = ContextManager(system="你是助手")
    asm = PromptAssembler(base="你是助手", registry=reg)

    def llm(messages, tools):
        calls["n"] += 1
        return LLMResponse(
            tool_calls=[ToolCall(id="c1", name="job_scout", arguments={})]
        )

    loop = AgentLoop(
        llm=llm,
        registry=reg,
        assembler=asm,
        context=ctx,
        max_rounds=8,
        early_return_markers=("【job_scout已完成】",),
    )
    out = loop.run("搜岗")
    assert out == done_text
    assert calls["n"] == 1
    assert "最大轮数" not in out
    assert loop.last_stop_reason == "early_marker"


def test_loop_compacts_when_over_budget() -> None:
    events: list[tuple[str, Any]] = []

    def on_event(kind: str, data: Any) -> None:
        events.append((kind, data))

    def fake_counter(messages):
        return len(messages) * 10

    n = {"i": 0}

    def llm(messages, tools):
        n["i"] += 1
        if n["i"] >= 3:
            return LLMResponse(content="done")
        return LLMResponse(
            tool_calls=[
                ToolCall(id=f"c{n['i']}", name="add", arguments={"a": 1, "b": 1})
            ]
        )

    reg = ToolRegistry()
    reg.register(make_add_tool())
    ctx = ContextManager(system="s", max_tokens=40, count_tokens=fake_counter)
    asm = PromptAssembler(base="s", registry=reg)
    loop = AgentLoop(
        llm=llm,
        registry=reg,
        assembler=asm,
        context=ctx,
        max_rounds=8,
        max_repeated_tool=10,
    )
    assert loop.run("go", on_event=on_event) == "done"
    compacted = [e for e in events if e[0] == "context_compacted"]
    assert compacted
    assert all(isinstance(e[1], int) and e[1] >= 0 for e in compacted)

    msgs = ctx.messages()
    ids = {
        tc["id"]
        for m in msgs
        if m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    for m in msgs:
        if m["role"] == "tool":
            assert m["tool_call_id"] in ids
