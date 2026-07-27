"""【手写】TDD：实现 agent/subagent.py 各空后，测试应变绿。

跑法:
  pytest tests/test_agent_subagent.py -q
"""

from __future__ import annotations

from repo2resume.agent.loop import LLMResponse, ToolCall
from repo2resume.agent.subagent import SubAgentRunner, SubAgentSpec
from repo2resume.agent.tools import Tool, ToolRegistry


def _echo_tool() -> Tool:
    """子 agent 可用的简单工具：把 text 原样返回。"""
    from pydantic import BaseModel, Field

    class EchoParams(BaseModel):
        text: str = Field(description="echo")

    return Tool(
        name="echo",
        description="Echo text back.",
        params_model=EchoParams,
        handler=lambda text: text,
        risk="readonly",
    )


def test_summarize_truncates_long_output() -> None:
    """空 3：超长输出截断并带标记。"""
    spec = SubAgentSpec(
        name="t",
        description="d",
        system_prompt="s",
        result_max_chars=20,
    )
    runner = SubAgentRunner(spec, llm=lambda m, t: LLMResponse(content="x"))
    out = runner._summarize_result("abcdefghijklmnopqrstuvwxyz")
    assert len(out) > 20  # 含截断标记
    assert out.startswith("abcdefghijklmnopqrst")
    assert "截断" in out


def test_summarize_keeps_short_output() -> None:
    """空 3：短输出原样返回。"""
    spec = SubAgentSpec(
        name="t",
        description="d",
        system_prompt="s",
        result_max_chars=100,
    )
    runner = SubAgentRunner(spec, llm=lambda m, t: LLMResponse(content="x"))
    assert runner._summarize_result("  hello  ") == "hello"


def test_run_uses_isolated_context_and_returns_final_text() -> None:
    """空 2：子 loop 跑通；主测用假 LLM：先调 echo，再给最终文本。"""
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="echo expert",
        system_prompt="你是 echo 专家",
        tools=[echo],
        max_rounds=5,
    )
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        # 子 agent 的 tools schema 应只包含 echo，不应有主 agent 的其它工具
        names = [t["function"]["name"] for t in tools]
        assert names == ["echo"]
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "ping"})]
            )
        # 第二轮应看到 tool 结果
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["content"] == "ping"
        return LLMResponse(content="子 agent 完成：ping")

    runner = SubAgentRunner(spec, llm=llm)
    out = runner.run("请 echo ping")
    assert out == "子 agent 完成：ping"
    assert calls["n"] == 2


def test_run_does_not_share_history_across_calls() -> None:
    """空 2：两次 run 上下文隔离——第二次不应看到第一次的 user 消息。"""
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="d",
        system_prompt="s",
        tools=[echo],
        max_rounds=3,
    )
    seen_first_user: list[bool] = []

    def llm(messages, tools):
        user_texts = [m.get("content") for m in messages if m.get("role") == "user"]
        # 每次 run 只有当前 task 这一条 user（system 另算）
        if "task-2" in (user_texts[-1] or ""):
            seen_first_user.append("task-1" in "".join(user_texts))
        return LLMResponse(content="ok")

    runner = SubAgentRunner(spec, llm=llm)
    runner.run("task-1")
    runner.run("task-2")
    assert seen_first_user == [False], "第二次 run 不得残留 task-1"


def test_as_tool_registers_and_invokes_run() -> None:
    """空 4：as_tool 名称/描述来自 spec；call 等价于 run。"""
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="repo_analyst",
        description="分析仓库的专家",
        system_prompt="s",
        tools=[echo],
        max_rounds=3,
    )

    def llm(messages, tools):
        return LLMResponse(content="摘要结果")

    runner = SubAgentRunner(spec, llm=llm)
    tool = runner.as_tool()
    assert tool.name == "repo_analyst"
    assert "专家" in tool.description

    main_reg = ToolRegistry()
    main_reg.register(tool)
    # 主 registry 调子 agent 工具
    result = main_reg.call("repo_analyst", {"task": "分析一下"})
    assert result == "摘要结果"
