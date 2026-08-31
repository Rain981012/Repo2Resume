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
    make_job_scout_subagent_tool,
    make_search_jobs_tool,
    make_set_job_prefs_tool,
)
from repo2resume.agent.context import ContextManager
from repo2resume.agent.hooks import ErrorRecoveryHook, PermissionHook, TraceHook
from repo2resume.agent.llm_adapter import make_llm_adapter
from repo2resume.agent.loop import AgentLoop
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.subagent import (
    SubAgentRunner,
    make_repo_analyst_spec,
)
from repo2resume.agent.tools import ToolRegistry
from repo2resume.chat_ui.confirm import make_confirm
from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database

logger = logging.getLogger(__name__)

# 薄提示：细则在工具返回值 / Writer-Critic 管线，勿在此堆业务规则。
CHAT_SYSTEM_PROMPT = (
    "你是 Repo2Resume 简历助手。回答用中文；数字与 URL 只引用工具返回，禁止编造。\n"
    "工具顺序（硬依赖，缺步会被工具拒）：\n"
    "1) repo_analyst — 用户说「分析仓库」时调用（默认 ./local_repos/，不要向用户索要路径）。"
    "工具返回含【repo_analyst已完成】时：必须把其中的统计与「方向」块展示给用户，"
    "请用户确认方向；禁止再问路径；不要回到自我介绍。"
    "同一轮里拿到【repo_analyst已完成】后不要立刻再调；用户明确要求「重新分析」时可再调。\n"
    "2) set_job_prefs — 用户确认方向后写入偏好（可与已有项合并，不必每次重传全部）。"
    "城市：用户说「全国/都行」→ city='全国' 且 remote=true；"
    "用户说北上广深/江浙沪 → 展开成具体城市名，"
    "city='北京,上海,广州,深圳' 这种逗号分隔，禁止写缩写；"
    "校招：用户说「都行/不限」→ is_campus=null，不要反复追问。"
    "拿到【已保存】后必须立刻 job_scout，禁止连续多次 set_job_prefs。\n"
    "3) job_scout — prefs 就绪后可调用；拿到【job_scout已完成】后必须把 Markdown "
    "**原样**展示给用户（综合 Top-N、匹配点/偏好/薪资/缺口、链接、职位编号），"
    "不要改写成一段理由，本轮不要马上再调 job_scout。"
    "【重要】搜岗次数没有终身上限。用户说「重新搜索 / 再搜 / 换一批」时必须再调 job_scout；"
    "禁止编造「系统规定只能搜一次」之类规则。\n"
    "4) generate_resume(job_id=…) — 用户选定序号后调用；内部已含写作审稿，"
    "勿自己写长简历；同一轮返回【已完成】后不要连打；用户要求重生成时可再调。\n"
    "打招呼/自我介绍不调工具。mock 公司名须标明示例岗。"
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
    reg.register(make_set_job_prefs_tool(db))

    search_tool = None
    find_tool = None
    llm_client = LLMClient(cfg, cache=cache)
    try:
        from repo2resume.retrieval.embedder import build_embedder

        embedder = build_embedder(cfg.embed_model)
        search_tool = make_search_jobs_tool(cfg, db, embedder)
        find_tool = make_find_project_materials_tool(cfg, db, embedder)
        reg.register(make_generate_resume_tool(cfg, db, embedder, llm=llm_client))
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
    trace_hook = TraceHook(db, session_id=session_id)
    reg.add_hook(trace_hook)
    reg.add_hook(ErrorRecoveryHook())

    def _llm_trace_sink(usage: Any) -> None:
        db.record_llm_trace(
            session_id,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            usage.latency_ms,
            payload={
                "cache_hit": getattr(usage, "cache_hit", False),
                "tool_names": getattr(usage, "tool_names", None),
            },
        )

    llm = make_llm_adapter(
        llm_client,
        trace_sink=_llm_trace_sink,
        # on_token 暂时禁用 streaming，排查卡死问题
    )

    analyst = SubAgentRunner(
        make_repo_analyst_spec(
            analyze_tool,
            return_after_tools=cfg.subagent_return_after_tools,
        ),
        llm=llm,
        tool_hooks=[trace_hook],
    )
    console.print(
        f"[dim]repo_analyst return_after_tools={cfg.subagent_return_after_tools} "
        f"(False=多步总结；True=回传工具原文)[/dim]"
    )
    reg.register(analyst.as_tool())
    if search_tool is not None:
        reg.register(
            make_job_scout_subagent_tool(
                search_tool,
                find_tool,
                llm=llm,
                tool_hooks=[trace_hook],
            )
        )
        console.print("[dim]job_scout=SubAgentRunner（失败则直调 search_jobs）[/dim]")

    loop = AgentLoop(
        llm=llm,
        registry=reg,
        assembler=assembler,
        context=ctx,
        max_rounds=8,
        # 允许「失败重试一次」；真正死循环仍会在第 3 次同名工具时停
        max_repeated_tool=3,
        # 搜岗成功后直接展示列表，避免弱模型再 set_job_prefs / 重复 job_scout 耗尽轮数
        early_return_markers=("【job_scout已完成】",),
    )
    return ChatRuntime(
        registry=reg,
        assembler=assembler,
        context=ctx,
        loop=loop,
        chat_ui=chat_ui,
    )
