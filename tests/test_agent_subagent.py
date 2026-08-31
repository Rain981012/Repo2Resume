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


def test_make_repo_analyst_spec_wraps_analyze_only() -> None:
    """工厂：Spec 只持有传入的 analyze 工具，名称固定为 repo_analyst。"""
    from pydantic import BaseModel, Field

    from repo2resume.agent.subagent import make_repo_analyst_spec

    class Params(BaseModel):
        x: str = Field(default="")

    analyze = Tool(
        name="analyze_repo",
        description="analyze",
        params_model=Params,
        handler=lambda **_: "ok",
        risk="readonly",
    )
    spec = make_repo_analyst_spec(analyze)
    assert spec.name == "repo_analyst"
    assert [t.name for t in spec.tools] == ["analyze_repo"]
    assert "authors" in spec.system_prompt
    assert spec.return_after_tools is False
    assert spec.max_repeated_tool == 3

    passthrough = make_repo_analyst_spec(analyze, return_after_tools=True)
    assert passthrough.return_after_tools is True
    assert passthrough.max_repeated_tool == 2


def test_make_job_scout_spec_wraps_search_and_find() -> None:
    """工厂：job_scout 持有 search_jobs + find_project_materials。"""
    from pydantic import BaseModel, Field

    from repo2resume.agent.subagent import make_job_scout_spec

    class Params(BaseModel):
        q: str = Field(default="")

    search = Tool(
        name="search_jobs",
        description="search",
        params_model=Params,
        handler=lambda **_: "jobs",
        risk="readonly",
    )
    find = Tool(
        name="find_project_materials",
        description="find",
        params_model=Params,
        handler=lambda **_: "hits",
        risk="readonly",
    )
    spec = make_job_scout_spec(search, find)
    assert spec.name == "job_scout"
    assert [t.name for t in spec.tools] == ["search_jobs", "find_project_materials"]
    assert "search_jobs" in spec.system_prompt

    solo = make_job_scout_spec(search)
    assert [t.name for t in solo.tools] == ["search_jobs"]
    assert solo.return_after_tools is True
    assert solo.max_repeated_tool == 2

    multi = make_job_scout_spec(search, return_after_tools=False)
    assert multi.return_after_tools is False
    assert multi.max_repeated_tool == 3


def test_run_recovers_from_inner_tool_validation_error() -> None:
    """子 registry 挂 ErrorRecovery：参数校验失败不炸穿，回填文本后子 agent 可继续。"""
    from pydantic import BaseModel, Field

    class StrictParams(BaseModel):
        items: list[str] = Field(description="must be list")

    tool = Tool(
        name="strict",
        description="strict list",
        params_model=StrictParams,
        handler=lambda items: f"ok:{items}",
        risk="readonly",
    )
    spec = SubAgentSpec(
        name="bot",
        description="d",
        system_prompt="s",
        tools=[tool],
        max_rounds=5,
    )
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            # 故意传错类型，触发 ValidationError
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="strict", arguments={"items": 123})]
            )
        content = messages[-1].get("content") or ""
        assert "失败" in content or "ValidationError" in content
        return LLMResponse(content="已从错误恢复")

    out = SubAgentRunner(spec, llm=llm).run("go")
    assert out == "已从错误恢复"
    assert calls["n"] == 2


def test_return_after_tools_passthrough_skips_second_llm() -> None:
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="d",
        system_prompt="s",
        tools=[echo],
        max_rounds=5,
        max_repeated_tool=2,
        return_after_tools=True,
    )
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        return LLMResponse(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "raw-tool"})]
        )

    runner = SubAgentRunner(spec, llm=llm)
    out = runner.run("go")
    assert out == "raw-tool"
    assert calls["n"] == 1
    assert runner.last_trace is not None
    assert runner.last_trace.outcome == "tool_passthrough"
    assert runner.last_trace.llm_calls == 1
    assert runner.last_trace.tool_names == ["echo"]


def test_return_after_tools_false_does_second_llm_summary() -> None:
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="d",
        system_prompt="s",
        tools=[echo],
        max_rounds=5,
        max_repeated_tool=3,
        return_after_tools=False,
    )
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "raw-tool"})]
            )
        assert messages[-1]["role"] == "tool"
        return LLMResponse(content="总结：raw-tool")

    runner = SubAgentRunner(spec, llm=llm)
    out = runner.run("go")
    assert out == "总结：raw-tool"
    assert calls["n"] == 2
    assert runner.last_trace is not None
    assert runner.last_trace.outcome == "llm_summary"
    assert runner.last_trace.llm_calls == 2
    assert runner.last_trace.return_after_tools is False


def test_repo_analyst_summary_keeps_completion_marker() -> None:
    from pydantic import BaseModel, Field

    from repo2resume.agent.subagent import make_repo_analyst_spec

    class Params(BaseModel):
        x: str = Field(default="")

    analyze = Tool(
        name="analyze_repo",
        description="analyze",
        params_model=Params,
        handler=lambda **_: "【repo_analyst已完成】\n统计：5 仓",
        risk="readonly",
    )
    spec = make_repo_analyst_spec(analyze, return_after_tools=False)
    calls = {"n": 0}

    def llm(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="analyze_repo", arguments={})]
            )
        return LLMResponse(content="## 分析结果\n仓库 5 个")

    runner = SubAgentRunner(spec, llm=llm)
    out = runner.run("分析")
    assert out.startswith("【repo_analyst已完成】")
    assert "仓库 5 个" in out
    assert calls["n"] == 2


def test_return_after_tools_false_with_repeat_limit_1_false_stuck() -> None:
    """阈值=1 且关闭直传时，第一次调工具就会被判卡死——这就是必须同步调大阈值的原因。"""
    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="d",
        system_prompt="s",
        tools=[echo],
        max_rounds=5,
        max_repeated_tool=1,
        return_after_tools=False,
    )

    def llm(messages, tools):
        return LLMResponse(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]
        )

    runner = SubAgentRunner(spec, llm=llm)
    out = runner.run("go")
    assert "卡死" in out
    assert runner.last_trace is not None
    assert runner.last_trace.outcome == "stuck"


def test_subagent_tool_hooks_record_inner_tools(tmp_path) -> None:
    """子 registry 挂 TraceHook 后，内部 search/echo 应写入同一 session。"""
    from repo2resume.agent.hooks import TraceHook
    from repo2resume.storage.db import open_db

    echo = _echo_tool()
    spec = SubAgentSpec(
        name="echo_bot",
        description="d",
        system_prompt="s",
        tools=[echo],
        max_rounds=5,
        return_after_tools=True,
    )

    def llm(messages, tools):
        return LLMResponse(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "ping"})]
        )

    db = open_db(tmp_path / "sub.db")
    hook = TraceHook(db, session_id="sess-sub")
    out = SubAgentRunner(spec, llm=llm, tool_hooks=[hook]).run("go")
    assert "ping" in out
    rows = db.conn.execute(
        "SELECT tool_name FROM tool_traces WHERE session_id='sess-sub'"
    ).fetchall()
    assert [r[0] for r in rows] == ["echo"]


