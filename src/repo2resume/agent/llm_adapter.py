"""LLM adapter — 把 LLMClient 包成 loop 要的 llm(messages, tools) -> LLMResponse。

【AI 辅助】模块 — loop 定义了 LLMResponse/ToolCall 数据形状（agent/loop.py），
LLMClient 吐的是 litellm 原生对象，这里做翻译，让 loop 不依赖 litellm 类型。
"""

from __future__ import annotations

import json
from typing import Any

from repo2resume.agent.loop import LLMResponse, ToolCall
from repo2resume.llm.client import LLMClient


def make_llm_adapter(client: LLMClient, *, temperature: float = 0.2):
    """返回一个 loop 可直接注入的 llm(messages, tools) -> LLMResponse 函数。"""

    def llm(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        content, raw_tool_calls = client.complete_with_tools(
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
        # 有 tool_calls 时按协议优先返回 tool_calls；content 留空避免 loop 误判为最终回复
        if tool_calls:
            return LLMResponse(content=None, tool_calls=tool_calls)
        return LLMResponse(content=content, tool_calls=[])

    return llm
