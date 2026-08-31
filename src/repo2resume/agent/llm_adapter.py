"""LLM adapter — 把 LLMClient 包成 loop 要的 llm(messages, tools) -> LLMResponse。

【AI 辅助】模块 — loop 定义了 LLMResponse/ToolCall 数据形状（agent/loop.py），
LLMClient 吐的是 litellm 原生对象，这里做翻译，让 loop 不依赖 litellm 类型。

方案 2（Phase 2.5 观测）：complete_with_tools 返回 (content, raw_tool_calls, usage)，
adapter 拿 usage 调 trace_sink 落库——LLMClient 不碰 db，保持可被非 agent 调用方复用。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from repo2resume.agent.loop import LLMResponse, ToolCall
from repo2resume.llm.client import LLMClient, LLMUsage


def make_llm_adapter(
    client: LLMClient,
    *,
    temperature: float = 0.2,
    trace_sink: Callable[[LLMUsage], None] | None = None,
    on_token: Callable[[str], None] | None = None,
):
    """返回一个 loop 可直接注入的 llm(messages, tools) -> LLMResponse 函数。

    trace_sink: 每次成功 LLM 调用后调一次，传入 LLMUsage（model/tokens/cost/latency）。
        生产里传 `lambda u: db.record_llm_trace(session_id, u.model, ...)`；非 agent 调用方不传。
    on_token: 传了就走流式路径，每个文本 chunk 调 on_token(text)；
        不传则走非流式（带缓存 + fallback）。
    """

    def llm(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        if on_token is not None:
            content, raw_tool_calls, usage = client.complete_with_tools_stream(
                messages, tools, on_token=on_token, temperature=temperature
            )
        else:
            content, raw_tool_calls, usage = client.complete_with_tools(
                messages, tools, temperature=temperature
            )
        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "{}") if fn else "{}"
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(ToolCall(id=getattr(tc, "id", ""), name=name, arguments=arguments))
        if usage.tool_names is None:
            usage.tool_names = [tc.name for tc in tool_calls if tc.name]
        if trace_sink is not None:
            trace_sink(usage)
        # 有 tool_calls 时按协议优先返回 tool_calls；content 留空避免 loop 误判为最终回复
        if tool_calls:
            return LLMResponse(content=None, tool_calls=tool_calls)
        return LLMResponse(content=content, tool_calls=[])

    return llm
