"""Subagent Runtime — 子 agent 即工具（Phase 4 【手写】）。

【手写】填空题 — 把 pass / \"\"\"填空\"\"\" 换成真实代码，不要让 AI 整块生成实现体。

每做完一空跑: pytest tests/test_agent_subagent.py -q

================================================================================
整体心智模型（先读这段，再写代码）
================================================================================

Phase 1–3 是「单 agent」：一个 AgentLoop + 一套工具 + 一份长上下文。
主 agent 既要跟用户聊天，又要自己跑 analyze / search / 以后还要写简历——
上下文越来越杂，系统提示词互相抢注意力。

多智能体的关键洞察（DESIGN.md §4）：
  真正收益不是「更聪明」，而是：
  ① 每个子 agent 上下文更短、更聚焦（指令不被稀释）
  ② 可换不同模型（便宜模型过滤、贵模型写作）
  ③ 关注点分离，提示词好维护
  代价：token↑、延迟↑、信息在边界会丢、调试更难

本文件的实现方式（业界最干净的一种）：
  **子 agent 即工具（Subagent-as-Tool）**

  Orchestrator（主 chat loop）的工具列表里有例如 `run_repo_analyst`：
    → handler 内部 new 一个独立的 AgentLoop
    → 独立 ContextManager（不共享主会话历史！）
    → 独立 ToolRegistry（只给它 git/分析相关工具）
    → 独立 system prompt（「你是仓库分析专家…」）
    → loop.run(task) 跑完
    → 只把**摘要字符串**返回给主 agent（不要把子历史整包塞回去）

  主 agent 看到的是普通 tool 结果；它不知道也不关心里面套了另一个 loop。

和「再写一个普通函数工具」的区别：
  普通工具 = 确定性 Python（analyze_repo 挖仓）
  子 agent 工具 = 里面还有 LLM 决策循环（能多步调自己的工具再总结）

和 LangGraph Supervisor 的关系：
  思想同构（主管派下属）；我们用手写 loop 嵌套实现，不引入图框架。

================================================================================
属于 agent 开发的哪一部分？
================================================================================
harness 组件表里的 **Subagent Runtime**（DESIGN.md §1.2）。
依赖：AgentLoop / ContextManager / PromptAssembler / ToolRegistry（Phase 2）。
本文件不重写 loop，只「组装 + 摘要回传 + 暴露成 Tool」。

================================================================================
面试知识点（写代码前先能口述）
================================================================================

Q1: 什么是多智能体？和单 agent 多工具有何区别？
A: 多工具仍是一个会话、一份上下文。多智能体 = 多个独立 system/上下文/工具集，
   通过协作模式（Pipeline / Supervisor / Debate）配合。子 agent 即工具是 Supervisor
   的一种落地：下属对主管暴露成一个 function call。

Q2: 为什么子 agent 必须用独立 ContextManager，不能共用主会话？
A: 共用会：① 主会话的闲聊污染专家；② 子工具的中间步骤撑爆主上下文；
   ③ 权限/卡死检测边界混乱。独立上下文 = 关注点隔离的物理保证。

Q3: 为什么只回传摘要，不回传完整子轨迹？
A: 主 agent 只需要「结论」做决策；完整轨迹 token 贵且干扰。需要细节时让主 agent
   再派一次更窄的任务，或把关键数字写进摘要。这是「信息边界」取舍。

Q4: Writer-Critic 算多智能体吗？和 Subagent-as-Tool 什么关系？
A: Writer-Critic 是角色分工 + 有限轮修订（也可以是同 loop 里两个提示词角色）。
   Subagent-as-Tool 是运行时隔离。本项目 Phase 4：分析/搜岗可拆成子 agent 工具；
   Writer-Critic 在 resume/ 里用循环 ≤2 轮（【AI 辅助】），二者互补。

Q5: 子 agent 失败了怎么办？
A: 子 loop 内部仍有 max_rounds / 卡死检测；异常应变成字符串摘要返回主 agent
   （或让 ErrorRecovery 处理外层 Tool.call），避免主对话直接崩。

================================================================================
本文件要你填的空（建议顺序）
================================================================================
空 1  SubAgentSpec          — 配置：名字、系统提示、工具集、max_rounds…
空 2  SubAgentRunner.run    — 组装独立 loop 并执行，返回摘要 str
空 3  _summarize_result     — （可选辅助）把过长输出截断/加前缀
空 4  SubAgentRunner.as_tool — 包装成 Tool，供主 Registry.register

不要一次写完；每空跑测试。AI 禁止替你填函数体。

================================================================================
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

# run() / as_tool() 已实现，这些符号均在使用中。
from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 空 0 相关类型（已给出，一般不用改）
# ---------------------------------------------------------------------------

LLMFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], LLMResponse]


@dataclass
class SubAgentSpec:
    """空 1：子 agent 的「身份证」——描述一个专家要长什么样。

    字段含义：
      name:           工具名（主 agent 调用时喊的名字），如 "repo_analyst"
      description:    给主 LLM 看的说明书（何时该派这个下属）
      system_prompt:  子 agent 自己的 system（专家人设），与主 chat 的 base 分离
      tools:          子 agent 能用的 Tool 列表（不要塞主 agent 的全部工具）
      max_rounds:     子 loop 轮数上限（应 ≤ 主 loop，防止套娃烧 token）
      max_repeated_tool: 子 loop 卡死检测阈值
      result_max_chars:  回传主 agent 的摘要最大字符数（防把长报告整包塞回）

    为什么是 dataclass 而不是随便 dict？
      字段固定、可类型检查；以后加 model_override 等也不会散落。

    伪代码 / 期望：
      spec = SubAgentSpec(
          name="repo_analyst",
          description="分析本地 git 仓库并返回技能/贡献摘要。",
          system_prompt="你是仓库分析专家。只使用给定工具，用中文简洁总结。",
          tools=[analyze_tool],
          max_rounds=6,
      )
    """

    name: str
    description: str
    system_prompt: str
    tools: list[Tool] = field(default_factory=list)
    max_rounds: int = 6
    max_repeated_tool: int = 3
    result_max_chars: int = 4000
    # True：工具执行后直接回传原文；False：再进一轮 LLM 总结（多步推理）
    return_after_tools: bool = False


def subagent_repeat_limit(return_after_tools: bool) -> int:
    """卡死阈值必须和 return_after_tools 一起改。

    True：几乎走不到卡死检测（工具后立刻 return），2 即可。
    False：第一次调工具后还要再进 LLM；若阈值是 1，repeat=1 会立刻误判卡死。
    """
    return 2 if return_after_tools else 3


@dataclass
class SubAgentRunTrace:
    """一次子代理 run 的可观测记录，供 TUNING_LOG / 对照实验引用。"""

    name: str
    return_after_tools: bool
    max_repeated_tool: int
    llm_calls: int
    tool_names: list[str]
    elapsed_ms: float
    outcome: str
    result_chars: int


class _SubAgentParams(BaseModel):
    """子 agent 作为 Tool 时，主 LLM 传入的参数。

    只要一个 task 字符串：主 agent 用自然语言把子任务说清楚即可。
    不要在这里塞 paths/authors——那是子 agent 自己调工具时的事。
    """

    task: str = Field(..., description="交给子 agent 的具体任务描述。")


class SubAgentRunner:
    """空 2–4：按 Spec 跑一个独立 AgentLoop，并可暴露成主 agent 的 Tool。

    用法（填完后）:
        runner = SubAgentRunner(spec, llm=fake_or_real_llm)
        summary = runner.run("分析 ./local_repos 里我的贡献")
        reg.register(runner.as_tool())
    """

    def __init__(
        self,
        spec: SubAgentSpec,
        *,
        llm: LLMFn,
        tool_hooks: list[Any] | None = None,
    ) -> None:
        self.spec = spec
        self._llm = llm
        self._tool_hooks = list(tool_hooks or [])
        self.last_trace: SubAgentRunTrace | None = None

    def run(self, task: str) -> str:
        """空 2：用独立上下文跑子 AgentLoop，返回给主 agent 的摘要字符串。

        -----------------------------------------------------------------------
        知识点：为什么每次 run 都要 new ContextManager？
          - 两次派同一专家不应共享上一次的中间 tool 轨迹（除非你显式做会话）。
          - MVP：无状态子 agent——每次任务从空白上下文开始，结果更好预期、更好测。

        -----------------------------------------------------------------------
        步骤（按序写，写完跑 test_subagent_run_isolated_context）:
          1) 建空 ToolRegistry，把 self.spec.tools 逐个 register
          2) ctx = ContextManager(system=self.spec.system_prompt)
             注意：这里的 system 会被 assembler 覆盖也行——关键是 messages 历史是空的
          3) asm = PromptAssembler(base=self.spec.system_prompt, registry=reg)
          4) loop = AgentLoop(
                 llm=self._llm,
                 registry=reg,
                 assembler=asm,
                 context=ctx,
                 max_rounds=self.spec.max_rounds,
                 max_repeated_tool=self.spec.max_repeated_tool,
             )
          5) raw = loop.run(task)
          6) return self._summarize_result(raw)

        伪代码：
          reg = ToolRegistry()
          for t in self.spec.tools:
              reg.register(t)
          ctx = ContextManager(system=self.spec.system_prompt)
          asm = PromptAssembler(base=self.spec.system_prompt, registry=reg)
          loop = AgentLoop(...)
          return self._summarize_result(loop.run(task))

        边界：
          - task 空字符串 → 可返回提示「task 不能为空」，或仍交给 loop（测试约定见测试文件）
          - loop 返回「卡死/最大轮数」文案 → 原样经 summarize 回传，让主 agent 看见失败原因
        """
        from repo2resume.observability.langsmith_span import span_call

        return span_call(
            f"subagent.{self.spec.name}",
            lambda: self._run_isolated(task),
            run_type="chain",
            inputs={"task": (task or "")[:400]},
            outputs_of=lambda text: {"preview": text or ""},
        )

    def _run_isolated(self, task: str) -> str:
        import time

        from repo2resume.agent.hooks import ErrorRecoveryHook
        from repo2resume.agent.progress import emit_progress

        emit_progress(f"子代理 {self.spec.name} 开始：{task[:60]}")
        if self.spec.return_after_tools:
            emit_progress(f"子代理 {self.spec.name}：等待 LLM 选工具（完成后直接回传）…")
        else:
            emit_progress(f"子代理 {self.spec.name}：等待 LLM 决策（可多步调工具后再总结）…")
        reg = ToolRegistry()
        for tool in self.spec.tools:
            reg.register(tool)
        # Trace 必须在 ErrorRecovery 之前，失败被吞掉前先落库。
        for hook in self._tool_hooks:
            reg.add_hook(hook)
        # 子 registry 也要吞校验/工具异常，否则一次参数错会炸穿整个 repo_analyst。
        reg.add_hook(ErrorRecoveryHook())
        ctx = ContextManager(system=self.spec.system_prompt)
        asm = PromptAssembler(base=self.spec.system_prompt, registry=reg)
        loop = AgentLoop(
            llm=self._llm,
            registry=reg,
            assembler=asm,
            context=ctx,
            max_rounds=self.spec.max_rounds,
            max_repeated_tool=self.spec.max_repeated_tool,
            return_after_tools=self.spec.return_after_tools,
        )

        llm_calls = 0
        tool_names: list[str] = []

        def _on_event(kind: str, data: Any) -> None:
            nonlocal llm_calls
            if kind == "tool_start":
                tool_names.append(str(data))
                emit_progress(f"子代理 {self.spec.name} 调用工具：{data}")
            elif kind == "llm_response":
                llm_calls += 1
                if getattr(data, "tool_calls", None):
                    names = ", ".join(tc.name for tc in data.tool_calls)
                    emit_progress(f"子代理 {self.spec.name} 决定调用：{names}")

        t0 = time.perf_counter()
        raw = loop.run(task, on_event=_on_event)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = self._summarize_result(raw)
        # 二次总结常丢掉工具里的完成标记；主 chat 靠它决定展示方向、禁止再问路径。
        done_marker = "【repo_analyst已完成】"
        if (
            self.spec.name == "repo_analyst"
            and not self.spec.return_after_tools
            and "analyze_repo" in tool_names
            and done_marker not in summary
            and "卡死" not in summary
            and "最大轮数" not in summary
        ):
            summary = done_marker + "\n\n" + summary
        if "卡死" in (raw or ""):
            outcome = "stuck"
        elif "最大轮数" in (raw or ""):
            outcome = "max_rounds"
        elif self.spec.return_after_tools:
            outcome = "tool_passthrough"
        else:
            outcome = "llm_summary"
        self.last_trace = SubAgentRunTrace(
            name=self.spec.name,
            return_after_tools=self.spec.return_after_tools,
            max_repeated_tool=self.spec.max_repeated_tool,
            llm_calls=llm_calls,
            tool_names=tool_names,
            elapsed_ms=elapsed_ms,
            outcome=outcome,
            result_chars=len(summary),
        )
        logger.info(
            "subagent %s outcome=%s llm_calls=%s tools=%s elapsed_ms=%.0f return_after_tools=%s",
            self.spec.name,
            outcome,
            llm_calls,
            tool_names,
            elapsed_ms,
            self.spec.return_after_tools,
        )
        return summary

    def _summarize_result(self, raw: str) -> str:
        """空 3：把子 agent 输出收成适合回传主上下文的摘要。

        -----------------------------------------------------------------------
        知识点：信息边界（information boundary）
          子 agent 内部可以很啰嗦；跨边界只留主 agent 决策所需。
          截断优于静默丢弃关键信息——截断时在末尾加标记，主 agent 知道不完整。

        步骤：
          1) 若 raw 是 None 或非 str，先 str(raw)
          2) text = raw.strip()
          3) 若 len(text) <= self.spec.result_max_chars: return text
          4) 否则 return text[: self.spec.result_max_chars] + "\\n…[子 agent 输出已截断]"

        伪代码：
          text = (raw or "").strip()
          limit = self.spec.result_max_chars
          if len(text) <= limit:
              return text
          return text[:limit] + "\\n…[子 agent 输出已截断]"
        """
        text = (raw or "").strip()
        limit = self.spec.result_max_chars
        if len(text) <= limit:
            return text
        return text[:limit] + "\n…[子 agent 输出已截断]"

    def as_tool(self) -> Tool:
        """空 4：把本 Runner 暴露成主 agent 可 register 的 Tool。

        -----------------------------------------------------------------------
        知识点：Subagent-as-Tool
          主 LLM 的 tools= 列表里出现的是普通 JSON Schema；
          它发出 tool_call(name=spec.name, arguments={task: "..."})
          → ToolRegistry.call → handler(**params) → 其实跑的是 self.run(task)
          对主 loop 而言，与调 analyze_repo 无异——这就是「编排透明」。

        步骤：
          1) 定义 handler(task: str) -> str: return self.run(task)
          2) return Tool(
                 name=self.spec.name,
                 description=self.spec.description,
                 params_model=_SubAgentParams,
                 handler=handler,
                 risk="readonly",  # MVP：子 agent 默认只读；若内部有 network 工具再另议
             )

        期望：
          tool = runner.as_tool()
          assert tool.name == runner.spec.name
          tool.call({"task": "hi"}) == runner.run("hi")
        """
        """
        填空: 按上面 1–2 步实现
        """

        def handler(task: str) -> str:
            return self.run(task)

        return Tool(
            name=self.spec.name,
            description=self.spec.description,
            params_model=_SubAgentParams,
            handler=handler,
            risk="readonly",
        )


# ---------------------------------------------------------------------------
# 可选工厂（先别填；空 2–4 绿了再写。用于把现有 analyze 工具包进专家）
# ---------------------------------------------------------------------------


def make_repo_analyst_tool(analyze_tool: Tool) -> Tool:
    """可选直调入口（跳过嵌套 LLM）。生产 chat 默认用 SubAgentRunner + make_repo_analyst_spec。"""

    class RepoAnalystParams(BaseModel):
        task: str = Field(
            default="",
            description=(
                "自然语言任务（如「分析本地仓库」）；paths/authors 可省略，"
                "工具用 ./local_repos/ 与 config 身份。"
            ),
        )

    def handler(task: str = "") -> str:
        from repo2resume.agent.progress import emit_progress

        _ = task
        emit_progress("repo_analyst（直调）：开始 analyze_repo…")
        result = analyze_tool.handler()
        return result if isinstance(result, str) else str(result)

    return Tool(
        name="repo_analyst",
        description=(
            "分析本地 git 贡献并返回统计与方向块。"
            "当你需要完整分析/画像时调用；同一轮只调一次。"
        ),
        params_model=RepoAnalystParams,
        handler=handler,
        risk="readonly",
    )


def make_repo_analyst_spec(
    analyze_tool: Tool,
    *,
    return_after_tools: bool = False,
) -> SubAgentSpec:
    """仓库分析子 agent：独立 loop，只持有 analyze_repo。

    return_after_tools=False：调完工具后再让子 LLM 总结（多步）。
    True：直接回传工具原文，避免弱模型二次总结超时/胡写。
    阈值由 subagent_repeat_limit 绑定，False 时必须 >1。
    """
    if return_after_tools:
        system_prompt = (
            "你是 Repo Analyst 子代理。\n"
            "- 第一轮必须调用 analyze_repo（paths/authors 可省略）。\n"
            "- 不要闲聊。"
        )
    else:
        system_prompt = (
            "你是 Repo Analyst 子代理。\n"
            "- 第一轮必须调用 analyze_repo（paths/authors 可省略）。\n"
            "- 拿到工具结果后用中文做简短忠实总结：保留【repo_analyst已完成】、统计数字与方向块，"
            "不要编造工具未给出的数字或仓库。\n"
            "- 不要再调第二次 analyze_repo；不要闲聊。"
        )
    return SubAgentSpec(
        name="repo_analyst",
        description=(
            "委派给仓库分析专家：分析本地 git 贡献并返回摘要与方向块。"
            "默认已扫描 ./local_repos/，用户说「分析仓库」即可调用；"
            "拿到完成后先展示；用户要求「重新分析」时可再调用。"
        ),
        system_prompt=system_prompt,
        tools=[analyze_tool],
        max_rounds=3,
        max_repeated_tool=subagent_repeat_limit(return_after_tools),
        result_max_chars=12000,
        return_after_tools=return_after_tools,
    )


def make_job_scout_spec(
    search_jobs_tool: Tool,
    find_materials_tool: Tool | None = None,
    *,
    return_after_tools: bool = True,
) -> SubAgentSpec:
    """职位搜索专家：持有 search_jobs（+ 可选 find_project_materials）。

    主 chat 只 register(runner.as_tool())，不要再直接挂 search_jobs，避免绕过专家。
    默认 return_after_tools=True，保住【job_scout已完成】标记不被二次总结丢掉。
    """
    tools: list[Tool] = [search_jobs_tool]
    if find_materials_tool is not None:
        tools.append(find_materials_tool)

    return SubAgentSpec(
        name="job_scout",
        description=(
            "委派给职位搜索专家：按已保存偏好搜岗并返回匹配列表。"
            "拿到【job_scout已完成】后先向用户展示结果；用户要求重新搜索时可再调用。"
            "禁止声称「每人只能搜一次」。"
        ),
        system_prompt=(
            "你是 Job Scout。第一轮直接调用 search_jobs（query 可留空，source 用 auto）。\n"
            "- 若返回缺 prefs，原样转告，不要编造职位。\n"
            "- 不要闲聊；search_jobs 成功后不要再调第二次。\n"
            "- 最终回复必须原样保留工具返回的 Markdown（综合 Top-N、四段分析、链接、职位编号）。"
        ),
        tools=tools,
        max_rounds=3,
        max_repeated_tool=subagent_repeat_limit(return_after_tools),
        result_max_chars=12000,
        return_after_tools=return_after_tools,
    )
