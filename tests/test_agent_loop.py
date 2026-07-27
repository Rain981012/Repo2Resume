"""【手写】TDD：实现 agent/loop.py 各空后，测试应变绿。"""

from __future__ import annotations

from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse, ToolCall
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import ToolRegistry, make_add_tool


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


def test_max_rounds_stops_infinite_tool_calls() -> None:
    """LLM 一直要调工具 → 达到 max_rounds 强制停。"""

    def llm(messages, tools):
        return LLMResponse(tool_calls=[ToolCall(id="c", name="add", arguments={"a": 1, "b": 1})])

    loop = _make_loop(llm)  # max_rounds=5
    out = loop.run("无限调")
    assert "最大轮数" in out or "停止" in out


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
