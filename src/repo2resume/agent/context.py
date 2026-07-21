"""Context manager — 会话历史 + token 预算 + compaction。

【手写】填空题 — 把 pass / \"\"\"填空\"\"\" 换成真实代码，不要让 AI 整块生成。

每做完一空跑: pytest tests/test_agent_context.py -q

================================================================================
整体心智模型（先读这段，再写代码）
================================================================================

loop 每轮都要把「会话历史」喂给 LLM。但上下文窗口有上限（token 预算），
聊久了旧消息会撑爆窗口 → "context rot"：模型注意力被稀释、成本飙升、
甚至直接报错。所以需要一个 ContextManager 帮 loop 管这件事：

  1. 存消息：user / assistant / tool 三种 role，按顺序追加
  2. 产出 messages：每轮调 LLM 前要 system + 历史 一起发
  3. 估 token：发之前先看一眼会不会超预算
  4. 压缩（compaction）：超了就把最老的几轮丢掉，留个「已裁剪」标记

为什么 token 计数要可注入（count_tokens 参数）？
  真实用 litellm.token_counter(model=..., messages=...)，但测试里不想联网、
  不想依赖模型，所以注入一个假函数（按消息字数算）即可。
  这是「把外部依赖做成可注入」的常见手法，方便单测。

为什么 compaction 不是「调 LLM 总结」？
  MVP 阶段先用「丢最老 + 标记」的确定性策略，简单可测。
  真正的 summarize-based compaction 留到 Phase 2.5 / 进阶。

================================================================================
属于 agent 开发的哪一部分？
================================================================================
对应 harness engineering 的 **Context Management** 组件（DESIGN.md §1.2 表）。
和它并列的还有：Agent Loop / Tool Registry / Prompt Assembler / State Store /
Hooks / Permissions / Observability / Error Recovery / Subagent。
本文件只管「上下文窗口」这一件事，是 loop 的另一个依赖（loop 还依赖 tools.py）。

================================================================================
面试可能问的知识点（对照本文件能怎么答）
================================================================================

Q1: agent 聊久了上下文爆炸怎么办？
A: context rot 问题。解法分两档：
   - 确定性裁剪（本文件）：按 token 预算丢最旧轮次 + 插标记，简单可测。
   - 摘要式压缩（进阶）：把旧轮次让 LLM 总结成一段塞回去，保留语义但贵且可能丢信息。
   生产里常组合：先按窗口裁，超阈值再 summarize。

Q2: token 预算怎么估？为什么要在客户端估而不是只信 API？
A: 客户端先估能：① 避免发超长请求被拒 + 浪费往返；② 控成本（按 token 计费）；
   ③ 决定何时触发 compaction。估法：tiktoken / litellm.token_counter；
   估不准没关系，关键是「提前触发」，宁早勿晚。

Q3: 为什么把 count_tokens 做成可注入参数？
A: 把外部依赖（模型/网络）抽成函数参数，单测能注入假实现，不联网、不依赖模型版本。
   这是 dependency injection 在 agent 单测里的典型用法——loop / context 都该这么测。

Q4: tool 结果为什么要带 tool_call_id？
A: OpenAI function-calling 协议要求：assistant 发 tool_call（带 id）→ 你回填
   role="tool" 且 tool_call_id 对得上，模型才能把「我调的那次」和「这个结果」配对。
   漏了 id 或对不上，API 直接报错或模型混淆。

Q5: compaction 为什么只动历史、不动 system？
A: system 是稳定前缀（人设/规则），每轮都要原样发；裁它等于改人设。
   历史才是「可丢的部分」。所以本文件把 system 单独存，messages() 时再拼。

Q6: 裁剪后模型「忘了」前面说过的话怎么办？
A: 这正是 context rot 的代价。缓解：① 插「已裁剪」标记让模型知道有省略；
   ② 关键状态（画像/职位）落 State Store（SQLite），不靠会话记忆；
   ③ 需要精确回忆时走 RAG 检索，而不是塞进上下文。→ 引出 retrieval 阶段。

Q7: 你的 compaction 策略有什么坑？
A: ① 按「条数」裁而非按「轮次」裁，可能把一个 tool_call 和它的 tool 结果拆开，
   导致协议断裂（assistant 带 tool_calls 但对应 tool 结果被丢）→ API 报错。
   生产级实现要按「完整轮次」裁。② 只裁一次，极端小预算下仍可能超。
   这些是面试加分点：能说出自己实现的边界。

================================================================================
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ContextManager:
    """会话历史 + token 预算管理。

    用法（loop 里大致这样）:
        ctx = ContextManager(system="你是简历助手...", max_tokens=8000)
        ctx.add("user", "分析一下 ~/code/foo")
        # ... LLM 决策 → 调工具 → 回填 ...
        ctx.add_tool_result(tool_call_id="call_1", content="...")
        ctx.add("assistant", "...")
        ctx.maybe_compact()                       # 超预算就裁
        messages = ctx.messages()                  # 发给 LLM
    """

    def __init__(
        self,
        *,
        system: str,
        max_tokens: int = 8000,
        count_tokens: Callable[[list[dict[str, Any]]], int] | None = None,
    ) -> None:
        self._system = system
        self._max_tokens = max_tokens
        self._messages: list[dict[str, Any]] = []
        # 空 0（已完成）：默认用 litellm 计数；没传就用按字数粗估的假函数
        self._count_tokens = count_tokens or _char_count

    def add(self, role: str, content: str = "", *, tool_calls: list[dict] | None = None) -> None:
        """空 1：追加一条普通消息（user / assistant）。

        为什么需要它：loop 每轮都要往历史里加消息，统一入口好维护。
        期望产出: self._messages 多一个 {"role": role, "content": content}
        tool_calls: loop 执行工具时，assistant 消息要带上 tool_calls 字段
                    （OpenAI 协议要求，tool 结果才能配对）。None 时不加该字段。
        """
        """
        填空: self._messages.append({...})
        """
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """空 2：追加一条工具结果消息。

        为什么需要它：LLM 调工具后，结果要按 OpenAI 协议以 role="tool" 回填，
        并且必须带 tool_call_id 让模型对得上「我刚才调的是哪次」。
        期望产出: {"role": "tool", "tool_call_id": <id>, "content": <content>}
        """
        """
        填空: self._messages.append({"role":"tool", "tool_call_id":..., "content":...})
        """
        self._messages.append({"role":"tool", "tool_call_id": tool_call_id, "content": content})

    def messages(self) -> list[dict[str, Any]]:
        """空 3：返回要发给 LLM 的完整消息列表。

        为什么需要它：每轮调 LLM 前要 system + 历史 一起发。
        注意：system 是固定前缀，不放进 self._messages（便于 compaction 只动历史）。
        期望: [{"role":"system","content": self._system}, *self._messages]
        """
        """
        填空: return [{"role":"system","content":self._system}, *self._messages]
        """
        return [{"role":"system","content":self._system}, *self._messages]

    def token_count(self) -> int:
        """空 4：估算当前完整 messages 的 token 数。

        为什么需要它：发请求前先看会不会超预算，避免被 API 拒或烧钱。
        期望: self._count_tokens(self.messages())
        """
        """
        填空: return self._count_tokens(self.messages())
        """
        return self._count_tokens(self.messages())

    def maybe_compact(self, *, keep_last: int = 6) -> bool:
        """空 5：超预算就裁掉最旧的消息，返回是否裁过。

        为什么需要它：长会话不压缩就会 context rot。这里用最简单的策略——
        超预算时丢掉最旧的若干条，只保留最近 keep_last 条，并在历史开头
        插一条 system-ish 标记说明「前面 N 条已裁剪」，让模型知道有省略。

        策略（按这个写）:
          1) 若 token_count() <= max_tokens → 直接 return False
          2) 否则：把 self._messages 截成最后 keep_last 条，
             前面插一条 {"role":"system","content":"[已裁剪较早的对话]"}
             （注意：这条标记放进 self._messages，不是 self._system）
          3) return True

        边界: keep_last 不能小于 0；若裁完仍超（极小预算），就尽力而为，
              至少保证不无限循环——只裁一次就返回。
        """
        """
        填空: 见上面策略，三步走
        """
        if self.token_count() <= self._max_tokens:
            return False
        else:
            self._messages = self._messages[-keep_last:]
            self._messages.insert(0, {"role":"system","content":"[已裁剪较早的对话]"})
            return True


def _char_count(messages: list[dict[str, Any]]) -> int:
    """粗估 token：按总字符数 / 4 估（英文约 4 char ≈ 1 token）。

    测试默认用这个，避免依赖 litellm。真实运行时注入 litellm.token_counter。
    """
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
    return total // 4
