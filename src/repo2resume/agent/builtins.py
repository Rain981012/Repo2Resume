"""Built-in agent tools — 把 Phase 1/3 的分析、检索、职位能力包装成 Tool 注册进 loop。

【AI 辅助】模块 — 契约（Tool 抽象）已由 agent/tools.py 定好，这里只填实现。
关键设计：运行时依赖（config/cache/db）通过工厂函数闭包注入，不进 params schema。
"""

from __future__ import annotations

import contextvars
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from repo2resume.agent.tools import Tool
from repo2resume.analysis.git_miner import MineOptions, run_git
from repo2resume.analysis.pipeline import run_analyze
from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database
from repo2resume.storage.models import SkillProfile


def _coerce_str_list(value: Any) -> Any:
    """LLM 常把 list 参数序列化成 JSON 字符串（如 '["Rain"]'），在校验前掰回 list。"""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        return [text]
    return value


class AnalyzeRepoParams(BaseModel):
    """analyze_repo 的参数（给 LLM 看的 schema）。"""

    paths: list[str] = Field(
        default_factory=list,
        description="本地 git 仓库路径列表；为空则自动扫描当前目录下的 ./local_repos/",
    )
    authors: list[str] | None = Field(
        None,
        description=(
            '按邮箱或名字子串过滤作者，必须是 JSON 数组（如 ["Rain"]），不要传字符串。'
            "可省略：工具会用 config 里的 email / author_identities。"
        ),
    )
    since: str | None = Field(
        None,
        description=(
            "只统计此日期之后的提交，YYYY-MM-DD；不需要时省略或填 null，不要填字符串 'null'"
        ),
    )
    stats_only: bool = Field(
        False,
        description=(
            "False=生成完整技能画像（含 primary_direction，推荐）；"
            "True=只返回 git 统计摘要（省 LLM 成本）。"
        ),
    )

    @field_validator("paths", mode="before")
    @classmethod
    def _coerce_paths(cls, value: Any) -> Any:
        coerced = _coerce_str_list(value)
        return [] if coerced is None else coerced

    @field_validator("authors", mode="before")
    @classmethod
    def _coerce_authors(cls, value: Any) -> Any:
        return _coerce_str_list(value)


def _discover_local_repos(base: Path | None = None) -> list[Path]:
    """扫描 ./local_repos 下的 git 仓库（与 cli.discover_local_repos 同语义）。"""
    root = base if base is not None else Path.cwd() / "local_repos"
    if not root.is_dir():
        return []
    if (root / ".git").exists() or (root / ".git").is_file():
        return [root.resolve()]
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and ((child / ".git").exists() or (child / ".git").is_file()):
            found.append(child.resolve())
    return found


_AUTHOR_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+<([^>]+)>\s*$")


def _expand_authors_from_repos(repos: list[Path], patterns: list[str]) -> list[str]:
    """根据用户给的名字线索，在所有仓库里找出真实 git 身份。

    例如用户说“我是 rain”，传入 authors=["rain"]。工具自动扫描每个仓库的 shortlog，
    把名字或邮箱中含“rain”（不区分大小写）的真实作者（name + email）都加入过滤列表。
    这样“rain”就能同时匹配 Rain Wu 和 Rain981012 两个身份，不用用户手动配置邮箱。
    """
    matches: set[str] = set()
    for repo in repos:
        try:
            out = run_git(repo, "shortlog", "-sne", "--all", "--no-merges")
        except RuntimeError:
            continue
        for line in out.splitlines():
            m = _AUTHOR_RE.match(line)
            if not m:
                continue
            name = m.group(2).strip()
            email = m.group(3).strip()
            for pattern in patterns:
                pat = pattern.lower()
                if pat in name.lower() or pat in email.lower():
                    matches.add(name)
                    matches.add(email)
    return sorted(matches)


def _summarize_stats(stats: Any) -> str:
    """把 RepoStatsBundle 压成给 LLM 看的紧凑摘要（省 token）。"""
    lines: list[str] = []
    s = stats.summary
    lines.append(f"repo_count: {s.repo_count}")
    lines.append(f"total_author_commits: {s.total_author_commits}")

    lang_items = sorted(s.overall_language_share.items(), key=lambda kv: -kv[1])
    lang_str = ", ".join(f"{lang} {v:.1%}" for lang, v in lang_items[:6])
    lines.append(f"languages: {lang_str}")

    if stats.errors:
        lines.append(f"errors: {len(stats.errors)} repo(s) skipped")

    lines.append("repos:")
    for r in stats.repos:
        deps = ", ".join(sorted(r.dependencies.keys())) if r.dependencies else "-"
        caution = " [low_author_share]" if r.author_share < 0.15 else ""
        lines.append(
            f"  - {r.name}: commits={r.author_commits}/{r.total_commits} "
            f"share={r.author_share:.3f} deps={deps}{caution}"
        )

    return "\n".join(lines)


def make_analyze_tool(
    config: AppConfig,
    cache: CacheBackend | None,
    db: Database | None,
) -> Tool:
    """工厂：构造一个绑定了 config/cache/db 的 analyze_repo 工具。

    闭包注入运行时依赖，使 Tool 的 params schema 只暴露给 LLM 的字段。
    """

    def handler(
        paths: list[str] | None = None,
        authors: list[str] | None = None,
        since: str | None = None,
        stats_only: bool = False,
    ) -> str:
        repo_paths = [Path(p).expanduser() for p in (paths or [])]
        if not repo_paths:
            repo_paths = _discover_local_repos()
        if not repo_paths:
            return "未找到仓库：请提供 paths，或在当前目录下建 ./local_repos/ 放 git 仓库。"
        # 防御性清洗：模型常把「无值」填成字符串 "null" 或空串，统一当成 None
        if since and since.strip().lower() in {"null", "none", ""}:
            since = None
        # 作者默认值：优先用显式传入 → 再用 config 里登记的 git 身份列表 → 再退到 email/name。
        # 解决「跨仓库多个 git 身份」+「无作者过滤时假 100%」两个问题。
        # 防 LLM 误把用户自我介绍当 authors：
        #   如果 config.author_identities 已配置，且 LLM 传的 authors 是短名字（不含@、len<=3）
        #   则忽略它，直接用 config 中的身份。
        explicit = list(authors or [])
        if (
            config is not None
            and config.author_identities
            and explicit
            and all(len(a) <= 3 and "@" not in a for a in explicit)
        ):
            explicit = []
        author_list = explicit
        if config is not None and not author_list:
            author_list = list(config.author_identities)
        if not author_list and config is not None and config.email:
            author_list = [config.email]
        elif not author_list and config is not None and config.name:
            author_list = [config.name]
        if not author_list and config is not None:
            return (
                "未配置作者身份，无法计算你的真实贡献占比。请在 ~/.repo2resume/config.toml 里设 "
                'author_identities = ["邮箱1", "邮箱2"]，或调用时传 authors 参数。'
            )
        options = MineOptions(authors=author_list, since=since)
        stats, profile = run_analyze(
            repo_paths,
            options,
            config=config,
            cache=cache,
            db=db,
            stats_only=stats_only,
            use_cache=True,
        )
        if not stats_only and profile is not None:
            return _summarize_stats(stats) + f"\nprimary_direction: {profile.primary_direction}"
        return _summarize_stats(stats)

    return Tool(
        name="analyze_repo",
        description=(
            "分析本地 git 仓库：返回提交统计摘要，并默认生成技能画像（含 primary_direction）。"
            "paths 为空时自动扫描 ./local_repos/。"
            "用于回答「开始分析」「我适合什么方向」等。"
        ),
        params_model=AnalyzeRepoParams,
        handler=handler,
        risk="readonly",
    )


# ---------------------------------------------------------------------------
# Phase 3 tools: job search + project material retrieval
# ---------------------------------------------------------------------------


class SearchJobsParams(BaseModel):
    """search_jobs 参数。"""

    query: str = Field(
        default="",
        description=(
            "职位搜索关键词，如 'Python backend'。"
            "可留空：将自动使用最新技能画像的 primary_direction。"
        ),
    )
    count: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回职位数量。",
    )
    source: str = Field(
        default="auto",
        description=("职位来源：auto（Tavily→Bocha→mock）/ tavily / bocha / mock。"),
    )


class FindProjectMaterialsParams(BaseModel):
    """find_project_materials 参数。"""

    query: str = Field(
        default="",
        description="要检索的项目素材主题，如目标职位 JD 的关键职责。",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回素材条数。",
    )


def _load_latest_profile(db: Database | None) -> SkillProfile | None:
    """从 SQLite 读取最新一条技能画像。"""
    if db is None:
        return None
    row = db.conn.execute(
        "SELECT payload_json FROM skill_profiles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return SkillProfile.model_validate_json(row["payload_json"])


def _format_score(score: Any) -> str:
    """把匹配分数格式化为百分比字符串。"""
    try:
        return f"{float(score) * 100:.0f}%"
    except (TypeError, ValueError):
        return "N/A"


def make_search_jobs_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
) -> Tool:
    """工厂：构造 search_jobs 工具。"""
    from repo2resume.jobs.matcher import JobMatcher
    from repo2resume.jobs.search import search_jobs

    llm = LLMClient(config)
    matcher = JobMatcher(embedder, llm)

    def handler(query: str = "", count: int = 5, source: str = "auto") -> str:
        from repo2resume.agent.progress import emit_progress

        # query 为空：用 primary + secondary 多路搜索，覆盖画像全部方向
        profile = _load_latest_profile(db)
        if query.strip():
            queries = [query.strip()]
            directions_label = query.strip()
        else:
            if profile is None or not profile.primary_direction:
                return "请先运行 analyze 生成技能画像，或提供 query 参数，例如 'Python backend'。"
            queries = [profile.primary_direction, *profile.secondary_directions]
            seen_q: set[str] = set()
            uniq: list[str] = []
            for q in queries:
                q = (q or "").strip()
                if q and q not in seen_q:
                    seen_q.add(q)
                    uniq.append(q)
            queries = uniq
            directions_label = " / ".join(queries)

        emit_progress(f"搜岗开始（source={source}，{len(queries)} 组关键词，并发）")
        per_query = max(count, 3)
        seen_ids: set[str] = set()
        jobs: list[Any] = []

        def _search_one(q: str) -> tuple[str, list[Any]]:
            return q, search_jobs(q, count=per_query, config=config, db=db, source=source)

        workers = min(4, len(queries)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _search_one, q) for q in queries]
            for fut in as_completed(futures):
                try:
                    q, batch = fut.result()
                except Exception as exc:  # noqa: BLE001
                    emit_progress(f"检索失败：{exc}")
                    continue
                emit_progress(f"联网/本地检索完成：{q}（+{len(batch)}）")
                for j in batch:
                    if j.id not in seen_ids:
                        seen_ids.add(j.id)
                        jobs.append(j)
        if not jobs:
            return "未找到职位。请检查 Bocha/Tavily key，或直接粘贴 JD 文本给 generate_resume。"

        # 有联网结果时丢掉混进来的 mock（旧缓存 / 多轮合并）
        live = [j for j in jobs if j.source != "mock"]
        if live:
            jobs = live
        sources = sorted({j.source for j in jobs})
        source_note = "+".join(sources)
        if profile is None:
            lines = [
                f"找到职位（未加载画像；方向：{directions_label}；来源：{source_note}）：",
                "展示时必须带上每条「链接」字段给用户。",
                "用户选定序号后，主助手应 generate_resume(job_id=方括号内 id)，"
                "不要让用户去网站粘贴 JD。",
            ]
            for j in jobs[:count]:
                link = j.url or "（无链接）"
                snippet = (j.jd_text or "").replace("\n", " ").strip()
                if len(snippet) > 160:
                    snippet = snippet[:160] + "…"
                lines.append(
                    f"- [{j.id}] {j.title} @ {j.company or 'Unknown'}\n"
                    f"  链接：{link}\n"
                    f"  JD摘要：{snippet or '（无）'}"
                )
            return "\n".join(lines)

        emit_progress(f"已召回 {len(jobs)} 条，开始匹配打分…")
        # 控制 LLM 打分次数，避免对 10+ 列表页逐条打分拖到数分钟
        candidates = jobs[: max(count + 2, count)]
        top_k = max(count, min(len(candidates), count + 2))
        scores = matcher.match_all(profile, candidates, top_k=top_k)
        scores = scores[:count]
        lines = [
            f"按方向「{directions_label}」找到 {len(scores)} 个匹配职位"
            f"（来源：{source_note}；默认中文招聘站；primary: {profile.primary_direction}"
            + (
                f"；secondary: {', '.join(profile.secondary_directions)}"
                if profile.secondary_directions
                else ""
            )
            + "）：",
            "【展示契约】回复用户时每条职位必须包含可点击的「链接」URL；不要省略链接。",
            "【选岗契约】用户说「1」/「第2个」时，主助手必须立刻 "
            "generate_resume(job_id=该条方括号内 id)，使用库内已存 JD 摘要；"
            "禁止再让用户打开网页粘贴 JD（除非工具明确报 job_id 无效）。",
        ]
        if sources == ["mock"]:
            lines.append(
                "提示：当前为 mock 示例职位（StartupXYZ 等）。"
                "配置有效 Bocha/Tavily key 后重启 chat 再搜。"
            )
        for s in scores:
            job = next((j for j in candidates if j.id == s.job_id), None)
            title = job.title if job else s.job_id
            company = job.company if job else ""
            link = (job.url if job and job.url else "") or "（无有效链接）"
            snippet = ((job.jd_text if job else "") or "").replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:160] + "…"
            lines.append(
                f"- [{s.job_id}] {title} @ {company} | 匹配度 {_format_score(s.overall_score)} "
                f"(向量 {_format_score(s.vector_score)}, LLM {_format_score(s.llm_score)}) "
                f"| source={job.source if job else '?'}\n"
                f"  理由：{s.reason}\n"
                f"  链接：{link}\n"
                f"  JD摘要：{snippet or '（无）'}"
            )
        return "\n".join(lines)

    return Tool(
        name="search_jobs",
        description=(
            "基于当前技能画像搜索并匹配职位。返回多条职位、匹配分数与推荐理由。"
            "query 可留空：自动用 primary_direction + secondary_directions 多路搜索。"
            "默认 source=auto（Tavily → Bocha → mock）。"
        ),
        params_model=SearchJobsParams,
        handler=handler,
        risk="network",
    )


def make_find_project_materials_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
) -> Tool:
    """工厂：构造 find_project_materials 工具。"""
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.rerank import LLMReranker
    from repo2resume.retrieval.store import VectorStore

    llm = LLMClient(config)
    reranker = LLMReranker(llm)
    store = VectorStore(db, embedder) if db else None

    def handler(query: str = "", top_k: int = 5) -> str:
        if not query.strip():
            return "请提供 query 参数，例如目标职位 JD 中的关键职责。"
        if store is None:
            return "数据库未就绪，无法检索项目素材。"
        profile = _load_latest_profile(db)
        if profile is None:
            return "尚未生成技能画像，请先运行 analyze 或在 chat 中说「分析我的项目」。"

        # 保证画像素材已入索引（仅使用画像自带的 one_liners / highlights / tech_stack；
        # 如需更深的 README/commit 统计，可在 analyze 成功后手动重建索引）。
        store.upsert_profile_materials(profile, None)

        hits = hybrid_search(store, query, top_k=top_k * 2)
        if not hits:
            return "未找到与查询相关的项目素材。"
        reranked = reranker.rerank(query, hits, top_k=top_k)
        lines = [f"针对「{query}」召回 top-{len(reranked)} 项目素材："]
        for h in reranked:
            repo = h.metadata.get("repo", "unknown")
            lines.append(f"- [{repo}] {h.text[:200]} (score: {h.score:.3f})")
        return "\n".join(lines)

    return Tool(
        name="find_project_materials",
        description=(
            "根据查询主题从技能画像中召回最相关的项目素材（仓库摘要、亮点、技术栈、README）。"
            "用于按目标 JD 挑选可写进简历的经历。"
        ),
        params_model=FindProjectMaterialsParams,
        handler=handler,
        risk="readonly",
    )


class GenerateResumeParams(BaseModel):
    """generate_resume 参数。"""

    jd_text: str = Field(
        default="",
        description="目标职位 JD 全文或关键要求；与 job_id 至少填一个。",
    )
    job_id: str | None = Field(
        None,
        description="已搜索到的职位 id（来自 search_jobs）；可与 jd_text 二选一。",
    )
    output_path: str = Field(
        default="resume_draft.md",
        description="写出的 Markdown 路径（相对当前工作目录或绝对路径）。",
    )
    projects_only: bool = Field(
        default=False,
        description="True=只导出项目经历块；False=含姓名/技能摘要的完整稿。",
    )


def make_generate_resume_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
) -> Tool:
    """工厂：检索 + Writer-Critic + 渲染落盘。"""
    from repo2resume.resume.pipeline import run_resume_pipeline

    def handler(
        jd_text: str = "",
        job_id: str | None = None,
        output_path: str = "resume_draft.md",
        projects_only: bool = False,
    ) -> str:
        from repo2resume.agent.progress import emit_progress

        if db is None:
            return "数据库未就绪，无法生成简历。"
        profile = _load_latest_profile(db)
        if profile is None:
            return "尚未生成技能画像，请先运行 analyze / repo_analyst。"
        if not (jd_text or "").strip() and not job_id:
            return "请提供 jd_text（JD 文本）或 job_id（search_jobs 返回的职位 id）。"

        emit_progress("开始生成简历…")
        llm = LLMClient(config)
        try:
            result = run_resume_pipeline(
                config=config,
                db=db,
                embedder=embedder,
                llm=llm,
                profile=profile,
                jd_text=jd_text,
                job_id=job_id,
                output=Path(output_path),
                full_resume=not projects_only,
                strip_src=False,
            )
        except ValueError as exc:
            msg = str(exc)
            emit_progress(f"生成失败：{exc}")
            if "job id not found" in msg.lower() or "not found" in msg.lower():
                return (
                    f"生成失败：{exc}\n"
                    "提示：job_id 须为 search_jobs 返回的内部 id（如 bocha-xxxx），"
                    "不要用网页 URL 里的数字。"
                    "若刚搜过岗仍失败，请重新 job_scout 一次以写入职位库；"
                    "或直接传 jd_text。"
                )
            return f"生成失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            emit_progress(f"生成失败：{exc}")
            err = str(exc)
            if "超过" in err or "timeout" in err.lower() or "Timeout" in type(exc).__name__:
                return (
                    f"生成失败：{exc}\n"
                    "【不要自动重试 generate_resume】请把超时告知用户，"
                    "询问是否换模型/稍后再试，或先打开已有 resume_draft.md。"
                )
            return f"生成失败：{exc}\n【不要自动重试超过 1 次】若仍失败，向用户说明原因。"

        must_left = 0
        if result.reports:
            must_left = len(result.reports[-1].must_fix)
        approved = result.reports[-1].approved if result.reports else False
        preview = result.markdown[:1200]
        path = result.output_path or Path(output_path)
        # 明确「已完成」：避免 LLM 看到 approved=False / must>0 就整管重跑 generate_resume
        status = (
            "Critic 已通过"
            if approved
            else (
                f"Critic 仍有 {must_left} 条 must（草稿已落盘，可稍后自然语言改；"
                "不要再次调用 generate_resume）"
            )
        )
        return (
            f"【已完成】简历草稿已写入，勿再调用本工具。\n"
            f"draft_id={result.draft_id} → {path}\n"
            f"Critic 轮次={len(result.reports)} approved={approved} 末轮 must={must_left}\n"
            f"{status}\n"
            f"项目数={len(result.draft.projects)}\n\n"
            f"--- Markdown 预览 ---\n{preview}\n\n"
            "请把预览要点用中文告诉用户，并询问是否要改某一条。"
        )

    return Tool(
        name="generate_resume",
        description=(
            "针对目标 JD 生成可溯源项目经历（内部 Writer-Critic≤2 轮）并写成 Markdown。"
            "用户选定职位后优先传 job_id；会写磁盘需确认一次。"
            "返回「【已完成】」后禁止再次调用；超时/失败也不要连打多遍，应向用户汇报。"
        ),
        params_model=GenerateResumeParams,
        handler=handler,
        risk="write",
    )
