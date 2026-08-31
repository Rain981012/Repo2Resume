"""Agent Loop — 手写 tool-use 循环（全项目最该亲手写的文件）。

【手写】填空题 — 把 pass / \"\"\"填空\"\"\" 换成真实代码，不要让 AI 整块生成。

每做完一空跑: pytest tests/test_agent_loop.py -q

================================================================================
整体心智模型（先读这段，再写代码）
================================================================================

agent 的本质 = LLM + 循环 + 工具。本文件就是那个循环：

  用户输入
    → 组装上下文（system + 历史 + 工具 schema）
    → 调 LLM
    → LLM 决策：
        a) 直接回复文本   → 这就是最终答案，return
        b) 要调工具        → 执行工具 → 把结果回填进历史 → 再回「调 LLM」
    → 循环直到 (a) 或达到最大轮数

为什么这是核心？因为「agent」这个词的魔法全在这个循环里——模型只负责
「下一步干嘛」，循环负责「一直跑直到完成」。理解了这个，就理解了
Cursor/Claude Code/AutoGPT 的骨架。

为什么 LLM 要可注入（构造参数 llm）？
  和 context.py 的 count_tokens 同理：单测要能注入「剧本式」假 LLM，
  按顺序返回预设响应，不联网、不花钱。生产时注入 LLMClient 的适配函数。

数据形状（本文件定义，loop 和测试都用）:
  ToolCall(id, name, arguments)   —— 一次工具调用请求
  LLMResponse(content, tool_calls) —— LLM 的一轮返回
                                       content 有值 → 最终回复
                                       tool_calls 有值 → 要调工具

================================================================================
属于 agent 开发的哪一部分？
================================================================================
harness engineering 的 **Agent Loop** 组件（DESIGN.md §1.2 表）——核心中的核心。
依赖前面三个文件：tools.py（执行）、context.py（历史）、prompt_assembler.py（system）。
本文件把它们串成「会自主调工具的 agent」。

================================================================================
面试可能问的知识点（对照本文件能怎么答）
================================================================================

Q1: agent 到底是什么？和普通 LLM 调用有何区别？
A: 普通 LLM 调用 = 一问一答，无状态。agent = LLM + 循环 + 工具 + 上下文管理。
   模型每轮只决定「下一步干嘛」（回复 or 调工具），循环负责反复调用直到完成。
   本文件 run() 就是这个循环的最小实现。

Q2: 循环什么时候停？
A: 三个停止条件（本文件实现）：
   ① LLM 返回纯文本（没 tool_calls）→ 任务完成，return 内容。
   ② 达到 max_rounds → 防止无限循环，强制停（本文件返回提示语）。
   ③ （Phase 2.5 再加）工具异常 / 卡死检测。
   生产里 max_rounds 是硬上限，必加——模型可能反复调同一工具陷入死循环。

Q3: 为什么工具结果要回填进历史，而不是直接给模型看一眼？
A: 下一轮调 LLM 时，模型要看到「我上轮调了什么、拿到了什么」才能继续推理。
   回填进历史 = 让工具结果成为会话的一部分。这也意味着历史会膨胀 →
   需要 context.py 的 compaction。

Q4: 你的 loop 有什么坑 / 边界？
A: ① 模型反复调同一工具 → 死循环，靠 max_rounds 兜底（Phase 2.5 加重复检测）。
   ② 工具抛异常 → 目前会直接炸出去，Phase 2.5 加 error recovery（异常回传 LLM 重试）。
   ③ 没做权限/HITL → 写文件、联网工具应确认，Phase 2.5 加 pre-hook。
   能说出这些「还没做」的边界，比假装完美更得分。

Q5: 为什么不用 LangChain 的 AgentExecutor？
A: LangChain 是零件箱，快但黑盒；手写 loop 能掌握每一步分支、便于排障和面试讲清。
   生产两者都合理，看目标是「交付」还是「吃透」。本项目目标是吃透 harness。

================================================================================
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from repo2resume.agent.cancel import RunCancelled, check_cancelled
from repo2resume.agent.context import ContextManager
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import ToolRegistry


@dataclass
class ToolCall:
    """LLM 请求调用一次工具。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """LLM 一轮的返回。content 有值=最终回复；tool_calls 有值=要调工具。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class AgentLoop:
    """手写 tool-use 循环。

    用法:
        loop = AgentLoop(llm=my_llm, registry=reg, assembler=asm, context=ctx)
        answer = loop.run("分析一下 ~/code/foo", state={"phase":"analyze"})
    """

    def __init__(
        self,
        *,
        llm: Callable[[list[dict[str, Any]], list[dict[str, Any]]], LLMResponse],
        registry: ToolRegistry,
        assembler: PromptAssembler,
        context: ContextManager,
        max_rounds: int = 10,
        max_repeated_tool: int = 3,
        return_after_tools: bool = False,
        early_return_markers: tuple[str, ...] = (),
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._assembler = assembler
        self._context = context
        self._max_rounds = max_rounds
        # Phase 2.5：重复调用卡死检测。连续 max_repeated_tool 次调同一工具名 → 判定卡死，停。
        # 防模型反复调同一工具陷入死循环（max_rounds 是硬上限，但这个能更早、更准地停）。
        self._max_repeated_tool = max_repeated_tool
        # 子代理场景：工具跑完直接把结果当最终回复，省掉「再总结一轮 LLM」（易超时/胡写）。
        self._return_after_tools = return_after_tools
        # 主 chat：工具结果含约定标记（如【job_scout已完成】）时直接展示，避免弱模型继续空转耗轮数。
        self._early_return_markers = early_return_markers
        self._last_tool_texts: list[str] = []
        self._on_event: Callable[[str, Any], None] | None = None
        self.last_stop_reason: str = "final_text"
        self.last_stuck_detail: dict[str, Any] | None = None

    def _execute_tools(self, tool_calls: list[ToolCall]) -> None:
        """空 1：执行一批工具调用，把结果回填进 context。

        为什么需要它：LLM 说「我要调 add」后，loop 要真的去调，并把结果
        按 OpenAI 协议回填（role=tool + tool_call_id），下一轮 LLM 才能看到。
        步骤:
          for tc in tool_calls:
              result = self._registry.call(tc.name, tc.arguments)
              self._context.add_tool_result(tc.id, str(result))
        注意: 工具异常这里先不 catch（Phase 2.5 加 error recovery）。
        """
        """
        填空: for tc in tool_calls:
                  result = self._registry.call(tc.name, tc.arguments)
                  self._context.add_tool_result(tc.id, str(result))
        """
        self._last_tool_texts = []
        for tc in tool_calls:
            check_cancelled()
            if self._on_event:
                self._on_event("tool_start", tc.name)
            try:
                result = self._registry.call(tc.name, tc.arguments)
            except RunCancelled:
                self._context.add_tool_result(tc.id, "[用户中断：工具未完成]")
                # 同批尚未执行的工具也要回填，避免历史里悬空 tool_calls
                started = False
                for other in tool_calls:
                    if other.id == tc.id:
                        started = True
                        continue
                    if started:
                        self._context.add_tool_result(other.id, "[用户中断：未执行]")
                raise
            if self._on_event:
                self._on_event("tool_done", tc.name)
            text = str(result)
            self._last_tool_texts.append(text)
            self._context.add_tool_result(tc.id, text)

    def run(
        self,
        user_input: str,
        state: dict[str, Any] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
    ) -> str:
        """空 2 + 空 3 + 空 4：主循环。

        空 2 —— 准备：
          - 把用户输入存进 context（ctx.add("user", user_input)）
          - 用 assembler.build(state) 生成 system，赋给 ctx（ctx._system = ...）
            （MVP 假设一次 run 内 state 不变；每轮 system 一样）

        空 3 —— 循环 max_rounds 次，每轮：
          - messages = ctx.messages()
          - tools = self._registry.list_schemas()
          - resp = self._llm(messages, tools)
          - if resp.tool_calls:
                # 把 assistant 的 tool_calls 也存进历史（OpenAI 协议要求）
                tc_dicts = [{"id": tc.id, "type": "function",
                             "function": {"name": tc.name, "arguments": str(tc.arguments)}}
                            for tc in resp.tool_calls]
                ctx.add("assistant", "", tool_calls=tc_dicts)
                self._execute_tools(resp.tool_calls)
                continue   # ← 回到循环顶，再调 LLM
          - else:
                ctx.add("assistant", resp.content or "")
                return resp.content or ""

        空 4 —— 循环跑完仍没拿到最终回复（达到 max_rounds）：
          - return f"[达到最大轮数 {self._max_rounds}，停止]"
        """
        """
        填空: 按 空 2 / 空 3 / 空 4 三段写
        """
        from repo2resume.observability.bucket import infer_bucket
        from repo2resume.observability.langsmith_span import span_call
        from repo2resume.observability.run_context import (
            current_llm_model,
            current_run_id,
            current_session_id,
            enter_agent_turn,
            exit_agent_turn,
            git_sha,
        )

        enter_agent_turn()
        self.last_stop_reason = "final_text"
        self.last_stuck_detail = None
        try:
            return span_call(
                "agent.turn",
                lambda: self._run_turn(user_input, state, on_event),
                run_type="chain",
                inputs={"user": (user_input or "")[:400]},
                metadata={
                    "session_id": current_session_id() or "",
                    "obs_run_id": current_run_id() or "",
                    "git_sha": git_sha(),
                    "llm_model": current_llm_model() or "",
                },
                outputs_of=lambda text: {
                    "text": text or "",
                    "stop_reason": self.last_stop_reason,
                    "bucket": infer_bucket(stop_reason=self.last_stop_reason),
                    **({"stuck": self.last_stuck_detail} if self.last_stuck_detail else {}),
                },
            )
        finally:
            exit_agent_turn()
            from repo2resume.observability.repro import write_repro
            from repo2resume.observability.run_context import turn_depth

            if turn_depth() == 0:
                write_repro(extra={"stop_reason": self.last_stop_reason})

    def _run_turn(
        self,
        user_input: str,
        state: dict[str, Any] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
    ) -> str:
        # 空2
        self._context.add("user", user_input)
        self._context._system = self._assembler.build(state)
        self._on_event = on_event

        # 空 5（Phase 2.5 卡死检测）：跟踪连续同名工具调用次数
        # last_sig = 上一轮调用的工具名元组（sorted），repeat = 连续重复次数
        last_sig: tuple[str, ...] | None = None
        repeat = 0

        # 空3
        for _ in range(self._max_rounds):
            check_cancelled()
            n_before = len(self._context._messages)
            if self._context.maybe_compact():
                n_kept = max(len(self._context._messages) - 1, 0)
                dropped = n_before - n_kept
                from repo2resume.observability.langsmith_span import span_call as _span

                _span(
                    "context.compact",
                    lambda n=dropped: n,
                    outputs_of=lambda n: {"dropped": n},
                )
                if on_event:
                    on_event("context_compacted", dropped)
            messages = self._context.messages()
            tools = self._registry.list_schemas()
            resp = self._llm(messages, tools)
            check_cancelled()
            if on_event:
                on_event("llm_response", resp)
            if resp.tool_calls:
                tc_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": str(tc.arguments),
                        },
                    }
                    for tc in resp.tool_calls
                ]
                self._context.add("assistant", "", tool_calls=tc_dicts)
                try:
                    self._execute_tools(resp.tool_calls)
                except RunCancelled:
                    self._pad_missing_tool_results(resp.tool_calls)
                    self.last_stop_reason = "cancelled"
                    raise

                if self._return_after_tools and self._last_tool_texts:
                    self.last_stop_reason = "return_after_tools"
                    return "\n\n".join(self._last_tool_texts)

                if self._early_return_markers and self._last_tool_texts:
                    for text in self._last_tool_texts:
                        if any(marker in text for marker in self._early_return_markers):
                            self._context.add("assistant", text)
                            self.last_stop_reason = "early_marker"
                            return text

                # 空 5（Phase 2.5 卡死检测）：连续同名工具调用超阈值则停
                # 步骤:
                #   1) sig = tuple(sorted(tc.name for tc in resp.tool_calls))
                #   2) if sig == last_sig: repeat += 1
                #      else: last_sig = sig; repeat = 1
                #   3) if repeat >= self._max_repeated_tool:
                #          return f"[检测到连续 {repeat} 次重复调用 {sig}，判定卡死，停止]"

                sig = tuple(sorted(tc.name for tc in resp.tool_calls))
                if sig == last_sig:
                    repeat += 1
                else:
                    last_sig = sig
                    repeat = 1
                if repeat >= self._max_repeated_tool:
                    self.last_stop_reason = "stuck"
                    from repo2resume.observability.stuck import (
                        explain_stuck,
                        format_stuck_message,
                    )

                    self.last_stuck_detail = explain_stuck(
                        sig=sig,
                        repeat=repeat,
                        tool_texts=self._last_tool_texts,
                        early_markers=self._early_return_markers,
                    )
                    return format_stuck_message(self.last_stuck_detail)

                continue
            else:
                self._context.add("assistant", resp.content or "")
                self.last_stop_reason = "final_text"
                return resp.content or ""

        # 空4
        self.last_stop_reason = "max_rounds"
        return f"[达到最大轮数 {self._max_rounds}，停止]"

    def _pad_missing_tool_results(self, tool_calls: list[ToolCall]) -> None:
        """中断时补齐尚未回填的 tool 结果，避免下一轮历史协议破裂。"""
        done = {m.get("tool_call_id") for m in self._context._messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in done:
                self._context.add_tool_result(tc.id, "[用户中断：未执行]")
