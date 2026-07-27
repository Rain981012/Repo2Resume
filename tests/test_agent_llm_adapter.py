"""【AI 辅助】LLM 适配器单测：mock LLMClient，验证 litellm 响应→LLMResponse 翻译。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from repo2resume.agent.llm_adapter import make_llm_adapter


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _TC:
    id: str
    function: _Fn


class _FakeClient:
    def __init__(self, content, tool_calls):
        self._content = content
        self._tool_calls = tool_calls
        self.calls = 0

    def complete_with_tools(self, messages, tools, *, temperature=0.2):
        from repo2resume.llm.client import LLMUsage

        self.calls += 1
        usage = LLMUsage(model="fake", input_tokens=1, output_tokens=1, cost_usd=None, latency_ms=0)
        return self._content, self._tool_calls, usage


def test_adapter_translates_tool_calls() -> None:
    tc = _TC(id="c1", function=_Fn(name="add", arguments=json.dumps({"a": 1, "b": 2})))
    client = _FakeClient(content="", tool_calls=[tc])
    llm = make_llm_adapter(client)

    resp = llm([{"role": "user", "content": "go"}], [])
    assert resp.content is None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "c1"
    assert resp.tool_calls[0].name == "add"
    assert resp.tool_calls[0].arguments == {"a": 1, "b": 2}


def test_adapter_translates_plain_content() -> None:
    client = _FakeClient(content="你好", tool_calls=[])
    llm = make_llm_adapter(client)

    resp = llm([{"role": "user", "content": "hi"}], [])
    assert resp.content == "你好"
    assert resp.tool_calls == []


def test_adapter_handles_bad_arguments_json() -> None:
    tc = _TC(id="c1", function=_Fn(name="add", arguments="not-json"))
    client = _FakeClient(content="", tool_calls=[tc])
    llm = make_llm_adapter(client)

    resp = llm([], [])
    assert resp.tool_calls[0].arguments == {}  # 坏 JSON → 空 dict，不抛


def test_adapter_calls_trace_sink_with_usage() -> None:
    """方案 2：adapter 拿到 usage 后调 trace_sink，把成本信息交出去落库。"""
    seen = []
    client = _FakeClient(content="hi", tool_calls=[])
    llm = make_llm_adapter(client, trace_sink=lambda u: seen.append(u))

    llm([{"role": "user", "content": "x"}], [])
    assert len(seen) == 1
    assert seen[0].model == "fake"
    assert seen[0].input_tokens == 1
    assert seen[0].latency_ms == 0


def test_adapter_without_trace_sink_does_not_crash() -> None:
    """不传 trace_sink（非 agent 调用方）→ 不落库也不报错。"""
    client = _FakeClient(content="hi", tool_calls=[])
    llm = make_llm_adapter(client)  # trace_sink 默认 None
    resp = llm([{"role": "user", "content": "x"}], [])
    assert resp.content == "hi"
