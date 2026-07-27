"""【手写】Phase 2.5：AgentLoop 重复调用卡死检测。

LLM 反复调同一工具陷入死循环时，loop 应在连续 max_repeated_tool 次后主动停，
返回卡死提示语，而不是傻跑到 max_rounds。实现 loop.py 的 TODO 后本文件变绿。
"""

from __future__ import annotations

from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse, ToolCall
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import ToolRegistry, make_add_tool


def _assembler(reg: ToolRegistry) -> PromptAssembler:
    return PromptAssembler(base="test", registry=reg)


def _make_loop(script: list[LLMResponse], *, max_repeated_tool: int = 3) -> tuple[AgentLoop, dict]:
    """按剧本返回 LLM 响应的假 llm；calls 记录每轮被调次数。"""
    reg = ToolRegistry()
    reg.register(make_add_tool())
    ctx = ContextManager(system="sys")
    asm = _assembler(reg)
    calls = {"n": 0}

    def llm(messages, tools):
        i = calls["n"]
        calls["n"] += 1
        if i < len(script):
            return script[i]
        return LLMResponse(content="fallback")

    loop = AgentLoop(
        llm=llm,
        registry=reg,
        assembler=asm,
        context=ctx,
        max_rounds=20,
        max_repeated_tool=max_repeated_tool,
    )
    return loop, calls


def test_stops_on_repeated_same_tool() -> None:
    """连续 3 轮都调 add → 第 3 轮后判定卡死，返回提示语，不再继续。"""
    resp = LLMResponse(tool_calls=[ToolCall(id="c", name="add", arguments={"a": 1, "b": 1})])
    loop, calls = _make_loop([resp] * 10, max_repeated_tool=3)

    out = loop.run("test")
    assert "卡死" in out or "重复" in out or "stuck" in out.lower()
    # 不该把 20 轮跑满：3 次重复就停
    assert calls["n"] <= 4


def test_different_tools_not_stuck() -> None:
    """交替调不同工具不算卡死（这里用 add + 不同参数模拟，工具名相同但本测试关注同名连续）。"""
    # 同名工具连续但 max_repeated_tool=5，3 次后 LLM 给最终回复 → 正常返回
    r1 = LLMResponse(tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 1})])
    r2 = LLMResponse(tool_calls=[ToolCall(id="c2", name="add", arguments={"a": 2, "b": 2})])
    r3 = LLMResponse(content="done")
    loop, _ = _make_loop([r1, r2, r3], max_repeated_tool=5)
    assert loop.run("test") == "done"


def test_no_tool_calls_not_stuck() -> None:
    """纯文本回复轮不参与卡死计数。"""
    loop, _ = _make_loop([LLMResponse(content="hi")], max_repeated_tool=2)
    assert loop.run("test") == "hi"


def test_default_max_repeated_tool_is_3() -> None:
    """默认阈值 3。"""

    # 检查 __init__ 签名有默认值 3
    import inspect

    sig = inspect.signature(AgentLoop.__init__)
    assert sig.parameters["max_repeated_tool"].default == 3
