"""Chat 工具注册、hooks、system prompt、LLM adapter。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from repo2resume.agent.builtins import (
    make_analyze_tool,
    make_find_project_materials_tool,
    make_generate_resume_tool,
    make_search_jobs_tool,
)
from repo2resume.agent.context import ContextManager
from repo2resume.agent.hooks import ErrorRecoveryHook, PermissionHook, TraceHook
from repo2resume.agent.llm_adapter import make_llm_adapter
from repo2resume.agent.loop import AgentLoop
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.subagent import (
    SubAgentRunner,
    make_job_scout_spec,
    make_repo_analyst_spec,
)
from repo2resume.agent.tools import ToolRegistry
from repo2resume.chat_ui.confirm import make_confirm
from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database

logger = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = (
    "你是 Repo2Resume 的简历助手。可用工具：\n"
    "1) repo_analyst：委派给仓库分析专家。传入 task（自然语言），"
    "说明要分析什么、作者身份（如「我是 Rain」）、路径（可省略，默认 ./local_repos/）。"
    "同一轮用户请求只调用一次；成功后直接向用户汇报，失败则说明原因，不要反复重试。\n"
    "2) job_scout：委派给职位搜索专家。传入 task（如「按我的方向搜职位」"
    "或「只要 Python 后端」）；不要自己拼 search_jobs 参数。"
    "展示职位时必须带上工具返回的每条链接；默认中文招聘市场。\n"
    "3) generate_resume：针对 JD 或 job_id 生成可溯源项目经历并写入 Markdown"
    "（会触发写盘确认）。工具内部已含 Writer→Critic→修订，你只需调用一次；"
    "返回「【已完成】」或已写入路径后，直接向用户汇报预览，禁止再次 generate_resume"
    "（即使 Critic approved=False / must>0）。超时失败时告知用户，不要连打多遍。\n"
    "【何时调工具】用户明确要求分析/搜岗/生成简历时才调用。"
    "若只是打招呼、自我介绍（如「开始」「我是 Rain」「你好」），先用中文简介能力并询问下一步，"
    "不要调用任何工具。\n"
    "流程：先 repo_analyst → 告知方向并询问是否搜岗 → 确认后 job_scout → "
    "用户选定职位（如「1」「第2个」）后立刻 "
    "generate_resume(job_id=该条方括号内 id)，使用搜岗已入库的 JD；"
    "禁止再让用户打开招聘网站粘贴 JD。"
    "仅当工具报 job_id 无效且用户主动粘贴文本时，才改传 jd_text。\n"
    "不要编造数字或 URL；只引用工具返回内容。回答用中文。"
    "若出现 StartupXYZ/CloudScale 等 mock 公司名，须标明是示例岗。\n"
    "【贡献归属】用户质疑「是否本人写的/会不会贪功」时：不要只让用户手改；"
    "应再次 generate_resume(同一 job_id)，并在确认后说明将按本人 commit/"
    "低贡献仓收窄表述重新生成。"
)


@dataclass
class ChatRuntime:
    registry: ToolRegistry
    assembler: PromptAssembler
    context: ContextManager
    loop: AgentLoop
    chat_ui: dict[str, Any]


def build_chat_runtime(
    *,
    cfg: AppConfig,
    cache: CacheBackend,
    db: Database,
    session_id: str,
    console: Console,
    chat_ui: dict[str, Any],
) -> ChatRuntime:
    """装配主 agent：工具、hooks、子 agent、loop。"""
    reg = ToolRegistry()
    analyze_tool = make_analyze_tool(cfg, cache, db)

    search_tool = None
    find_tool = None
    try:
        from repo2resume.retrieval.embedder import build_embedder

        embedder = build_embedder(cfg.embed_model)
        search_tool = make_search_jobs_tool(cfg, db, embedder)
        find_tool = make_find_project_materials_tool(cfg, db, embedder)
        reg.register(make_generate_resume_tool(cfg, db, embedder))
        console.print(f"[dim]embedder loaded: {cfg.embed_model}[/dim]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load Phase 3 embedder: %s", exc)
        console.print(f"[yellow]Phase 3/4 tools disabled: {exc}[/yellow]")

    confirmed: set[str] = set()
    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write", "network"},
            confirm=make_confirm(console, chat_ui, confirmed),
        )
    )

    assembler = PromptAssembler(base=CHAT_SYSTEM_PROMPT, registry=reg)
    ctx = ContextManager(system=assembler.build(None))

    # PermissionHook → TraceHook → ErrorRecoveryHook（顺序有语义，勿改）
    reg.add_hook(TraceHook(db, session_id=session_id))
    reg.add_hook(ErrorRecoveryHook())

    def _llm_trace_sink(usage: Any) -> None:
        db.record_llm_trace(
            session_id,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            usage.latency_ms,
        )

    llm = make_llm_adapter(
        LLMClient(cfg, cache=cache),
        trace_sink=_llm_trace_sink,
        # on_token 暂时禁用 streaming，排查卡死问题
    )

    analyst = SubAgentRunner(make_repo_analyst_spec(analyze_tool), llm=llm)
    reg.register(analyst.as_tool())
    if search_tool is not None:
        scout = SubAgentRunner(
            make_job_scout_spec(search_tool, find_tool),
            llm=llm,
        )
        reg.register(scout.as_tool())

    loop = AgentLoop(
        llm=llm,
        registry=reg,
        assembler=assembler,
        context=ctx,
        max_rounds=8,
        max_repeated_tool=5,
    )
    return ChatRuntime(
        registry=reg,
        assembler=assembler,
        context=ctx,
        loop=loop,
        chat_ui=chat_ui,
    )
