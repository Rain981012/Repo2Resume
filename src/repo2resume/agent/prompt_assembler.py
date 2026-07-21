"""Prompt Assembler — 每轮动态拼 system prompt。

【手写】填空题 — 把 pass / \"\"\"填空\"\"\" 换成真实代码，不要让 AI 整块生成。

每做完一空跑: pytest tests/test_agent_prompt.py -q

================================================================================
整体心智模型（先读这段，再写代码）
================================================================================

system prompt 不是写死的字符串，而是「每轮重新构建的视图」。它由三块拼成：

  1. 基础指令（base）：你是谁、要遵守什么硬规则（防幻觉、HITL…）——稳定不变
  2. 当前状态摘要（state）：现在进行到哪一步、已有哪些产物（画像？职位？）
     ——每轮可能变，让模型知道自己处在流程的什么位置
  3. 工具说明（tools）：现在有哪些工具可调、各是干什么的——从 registry 拉

为什么不让 LLM 自己「记住」状态？
  会话历史会被 compaction 裁掉；状态如果只活在历史里，裁完就丢了。
  把状态显式塞进 system prompt，每轮都重新声明，模型才不会「失忆」。
  这和 Q6（裁剪后失忆）是同一个解法的另一面：关键信息放 system，不放历史。

和 context.py 的关系：
  loop 每轮:  system = assembler.build(state)
              ctx = ContextManager(system=system, ...)   # 或更新 ctx 的 system
  所以 PromptAssembler 产出的是「system 那条消息的 content」，交给 ContextManager。

================================================================================
属于 agent 开发的哪一部分？
================================================================================
harness engineering 的 **Prompt Assembler** 组件（DESIGN.md §1.2 表）。
和 Context Management / Tool Registry 并列，都是 loop 的依赖。
学到的点：提示词是「函数」而非「常量」——输入是当前状态，输出是给模型的指令。

================================================================================
面试可能问的知识点（对照本文件能怎么答）
================================================================================

Q1: system prompt 为什么要动态组装，不能写死？
A: 状态会变（已分析/已搜岗/已出简历）。写死就只能靠会话历史传状态，
   历史一裁就丢。动态组装 = 每轮把「当前状态 + 可用工具」重新声明进 system，
   模型始终知道自己在哪一步、能调什么。

Q2: 工具说明放 system 还是放 tools= 参数？区别？
A: 两条路都行：
   - 原生 function calling：放 tools=（结构化 JSON Schema），模型按协议调。
   - 纯文本 prompt（无 function calling 的模型）：把工具名+描述写进 system，
     让模型输出 JSON 文本，自己解析。
   本项目走 tools=（registry.list_schemas()），所以这里 _render_tools() 主要是
   「给模型一个人类可读的工具清单」做辅助，真正的可调用 schema 走 tools=。

Q3: 状态摘要该塞多少？
A: 够用即可——当前阶段 + 已有产物指针（如「已生成画像：Python 后端」），
   不要把整个画像 JSON 塞进 system（占 token、还和 tools 结果重复）。
   大块数据走 tool 结果留在历史/State Store，system 只放「索引级」摘要。

Q4: 为什么 base / state / tools 分开存？
A: 关注点分离：base 稳定（写一次）、state 易变（每轮更新）、tools 跟注册表走。
   改其中一块不动另两块；也方便单测各自验证。

================================================================================
"""

from __future__ import annotations

from typing import Any

from repo2resume.agent.tools import ToolRegistry


class PromptAssembler:
    """每轮把 base + state + tools 拼成 system prompt。

    用法（loop 里）:
        assembler = PromptAssembler(base="你是简历助手...", registry=reg)
        system = assembler.build(state={"phase": "analyze", "done": ["mine"]})
        ctx = ContextManager(system=system, ...)
    """

    def __init__(self, *, base: str, registry: ToolRegistry) -> None:
        self._base = base
        self._registry = registry

    def _render_state(self, state: dict[str, Any] | None) -> str:
        """空 1：把状态 dict 渲染成一段文本。

        为什么需要它：state 是结构化数据，LLM 要的是文本。这里做翻译。
        期望:
          - state 为 None 或空 → 返回 ""（不留空段）
          - 否则每项一行 "key: value"，拼成一段
        样例: {"phase":"analyze","done":["mine"]} →
              "phase: analyze\ndone: ['mine']"
        提示: 用 "\n".join(f"{k}: {v}" for k,v in state.items())；
              空 dict/None 返回 ""。
        """
        """
        填空: if not state: return ""
              return "\n".join(f"{k}: {v}" for k, v in state.items())
        """
        if not state:
            return ""
        lines = [f"{k}: {v}" for k, v in state.items()]
        return "\n".join(lines)

    def _render_tools(self) -> str:
        """空 2：从 registry 拉工具清单，渲染成人类可读文本。

        为什么需要它：让模型在 system 里看到「有哪些工具、各干什么」的概览
        （真正可调用的 schema 走 tools= 参数，这里只是辅助说明）。
        期望: 每个工具一行 "- <name>: <description>"
              没有工具 → 返回 ""
        提示: 遍历 registry._tools.values() 或加个 registry 的公开方法；
              本脚手架直接用 list_schemas() 也行（每个 schema 里有 name/description）。
        """
        """
        填空: lines = [f"- {s['function']['name']}: {s['function']['description']}"
                       for s in self._registry.list_schemas()]
              return "\n".join(lines)  （空列表自然得到 ""）
        """

        lines = []
        for s in self._registry.list_schemas():
            lines.append(f"- {s['function']['name']}: {s['function']['description']}")
        return "\n".join(lines)

    def build(self, state: dict[str, Any] | None = None) -> str:
        """空 3：拼出完整 system prompt = base + state 段 + tools 段。

        为什么需要它：loop 每轮调一次，拿到 system 字符串交给 ContextManager。
        期望结构（段之间空行分隔，空段不出现）:
          <base>

          [当前状态]
          <state 文本，若空则整段省略>

          [可用工具]
          <tools 文本，若空则整段省略>
        提示: 用一个 list 收集非空段，再 "\n\n".join(parts)。
              这样空段自动跳过，不用写一堆 if。
        """
        """
        填空: parts = [self._base]
              state_text = self._render_state(state)
              if state_text: parts.append("[当前状态]\n" + state_text)
              tools_text = self._render_tools()
              if tools_text: parts.append("[可用工具]\n" + tools_text)
              return "\n\n".join(parts)
        """
        parts = [self._base]
        state_text = self._render_state(state)
        if state_text: parts.append("[当前状态]\n" + state_text)
        tools_text = self._render_tools()
        if tools_text: parts.append("[可用工具]\n" + tools_text)
        return "\n\n".join(parts)
