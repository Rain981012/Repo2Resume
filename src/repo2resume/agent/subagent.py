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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from repo2resume.agent.context import ContextManager
from repo2resume.agent.loop import AgentLoop, LLMResponse
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import Tool, ToolRegistry

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

    def __init__(self, spec: SubAgentSpec, *, llm: LLMFn) -> None:
        self.spec = spec
        self._llm = llm

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
        """
        填空: 按上面 1–6 步实现
        """
        pass

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
        """
        填空: 按上面 1–4 步实现
        """
        pass

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
        pass


# ---------------------------------------------------------------------------
# 可选工厂（先别填；空 2–4 绿了再写。用于把现有 analyze 工具包进专家）
# ---------------------------------------------------------------------------


def make_repo_analyst_spec(analyze_tool: Tool) -> SubAgentSpec:
    """示例 Spec：仓库分析专家（仅持有 analyze_repo 一类工具）。

    你实现 SubAgentRunner 之后，可在 cli chat 里：
      spec = make_repo_analyst_spec(analyze_tool)
      runner = SubAgentRunner(spec, llm=主 llm 或更便宜的 llm)
      main_registry.register(runner.as_tool())
    并考虑是否仍直接 register 原始 analyze_repo（二选一，避免主 agent 绕过专家）。

    此函数体可后写；测试不依赖它。
    """
    return SubAgentSpec(
        name="repo_analyst",
        description=(
            "委派给仓库分析专家：分析本地 git 贡献并返回摘要。"
            "当你需要完整分析/画像而不想自己拼参数时调用。"
        ),
        system_prompt=(
            "你是 Repo Analyst。只使用提供的工具分析仓库。"
            "不要编造数字；用中文给出简洁摘要（含关键统计与主方向）。"
        ),
        tools=[analyze_tool],
        max_rounds=6,
    )
