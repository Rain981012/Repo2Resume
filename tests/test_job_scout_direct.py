from __future__ import annotations

from pydantic import BaseModel

from repo2resume.agent.builtins import make_job_scout_direct_tool
from repo2resume.agent.tools import Tool


class _SearchParams(BaseModel):
    query: str = ""
    source: str = "tavily"
    top_n: int | None = None
    count: int | None = None


def test_job_scout_direct_calls_search_jobs_auto() -> None:
    seen: dict[str, str] = {}

    def handler(
        query: str = "",
        source: str = "tavily",
        top_n: int | None = None,
        count: int | None = None,
    ) -> str:
        _ = top_n, count
        seen["query"] = query
        seen["source"] = source
        return "【job_scout已完成】ok"

    search = Tool(
        name="search_jobs",
        description="s",
        params_model=_SearchParams,
        handler=handler,
        risk="network",
    )
    out = make_job_scout_direct_tool(search).call({"task": "按偏好搜"})
    assert "【job_scout已完成】" in out
    assert seen["source"] == "auto"
    assert seen["query"] == ""


def test_job_scout_subagent_fallback_when_inner_llm_skips_tool() -> None:
    from repo2resume.agent.builtins import make_job_scout_subagent_tool
    from repo2resume.agent.loop import LLMResponse

    seen: list[str] = []

    def handler(
        query: str = "",
        source: str = "tavily",
        top_n: int | None = None,
        count: int | None = None,
    ) -> str:
        _ = query, top_n, count
        seen.append(source)
        return "【job_scout已完成】ok"

    search = Tool(
        name="search_jobs",
        description="s",
        params_model=_SearchParams,
        handler=handler,
        risk="network",
    )
    scout = make_job_scout_subagent_tool(
        search,
        llm=lambda _m, _t: LLMResponse(content="我先想想再搜"),
    )
    out = scout.call({"task": "搜岗"})
    assert "【job_scout已完成】" in out
    assert seen == ["auto"]


def test_job_scout_subagent_uses_inner_tool_call() -> None:
    from repo2resume.agent.builtins import make_job_scout_subagent_tool
    from repo2resume.agent.loop import LLMResponse, ToolCall

    def handler(
        query: str = "",
        source: str = "tavily",
        top_n: int | None = None,
        count: int | None = None,
    ) -> str:
        _ = query, top_n, count
        return f"【job_scout已完成】via-{source}"

    search = Tool(
        name="search_jobs",
        description="s",
        params_model=_SearchParams,
        handler=handler,
        risk="network",
    )

    def llm(_messages, _tools):
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="search_jobs",
                    arguments={"query": "", "source": "auto"},
                )
            ]
        )

    out = make_job_scout_subagent_tool(search, llm=llm).call({"task": "搜岗"})
    assert out.startswith("【job_scout已完成】via-auto")
