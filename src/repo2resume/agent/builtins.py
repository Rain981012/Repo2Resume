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
from repo2resume.jobs.bigtech import DEFAULT_BIG_TECH, official_company_name, official_site_queries
from repo2resume.jobs.relevance import (
    job_city_flag,
    looks_like_region_alias,
    split_pref_cities,
)
from repo2resume.jobs.salary import extract_salary_info
from repo2resume.jobs.site_rank import looks_like_job_title
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database
from repo2resume.storage.models import JobSearchPrefs, SkillProfile

CANDIDATE_BUDGET = 50
CANDIDATE_BUDGET_MULTIPLIER = 10  # top_n=5 时与 CANDIDATE_BUDGET 对齐
POOL_MULTIPLIER = CANDIDATE_BUDGET_MULTIPLIER  # backward-compatible alias
MAX_SEARCH_ROUNDS = 4
# 方向越多，CANDIDATE_BUDGET 摊到每个方向就越薄，最后每个方向都排不出东西
MAX_DIRECTIONS = 3
INITIAL_QUERY_CAP = 8
CAMPUS_QUERY_INSERT_AT = 1  # 官网 site: 插到最前；当前 6 家 Tavily 可检索官网能进首轮
MIN_RECOMMEND_SCORE = 0.50
QUERY_BATCH_SIZE = 4
# 展示契约：skill/prompts/02_job_search.md（单套综合 Top-N + 四段分析，默认不展示分数）
_COMPANY_HIRE_RE = re.compile(r"^(.{2,40}?)招聘")
_TITLE_COMPANY_RE = re.compile(r"招聘[_/·\-—](.+?)招聘\s*$")
PREFS_MISSING_MSG = (
    "【需先 set_job_prefs】尚未写入有效搜岗偏好，已拒绝联网搜索。\n"
    "请先向用户确认方向与偏好，再调用 set_job_prefs，缺项：{missing}\n"
    "必填：confirmed_directions；以及 city 或 remote 至少其一。"
)
CITY_ALIAS_MSG = (
    "【需先 set_job_prefs】city 还是区域缩写「{city}」，不能开搜。\n"
    "请把用户说的地区自己展开成具体城市名，逗号分隔后再调用 set_job_prefs。"
    "例如北上广深 → city='北京,上海,广州,深圳'。不要把缩写原样写入。"
)


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


def _format_direction_block(profile: SkillProfile) -> str:
    """用代码拼装方向确认块，避免主/子 agent 改写或漏字段。"""
    lines = [
        "## 方向（请用户确认后再 set_job_prefs）",
        f"primary: {profile.primary_direction}",
        "secondary: "
        + (", ".join(profile.secondary_directions) if profile.secondary_directions else "（无）"),
        "候选职位类型:",
    ]
    suggestions = profile.job_direction_suggestions[:8]
    if suggestions:
        for i, item in enumerate(suggestions, 1):
            lines.append(f"  {i}. {item.title} — {item.reason}")
    else:
        # 无 suggestions 时用 primary/secondary 兜底成类型列表
        fallback = [profile.primary_direction, *profile.secondary_directions]
        for i, title in enumerate([t for t in fallback if t], 1):
            lines.append(f"  {i}. {title} — 来自画像方向字段")
        if len(fallback) < 1:
            lines.append("  （无，请用户口述方向）")
    if profile.caution:
        lines.append("caution:")
        for c in profile.caution[:8]:
            lines.append(f"  - {c}")
    return "\n".join(lines)


def _format_stack_brief(profile: SkillProfile) -> str:
    """展示提炼后的主技术栈（避免全字段生硬直出）。"""
    stack = profile.tech_stack
    languages = stack.languages[:4]
    frameworks = stack.frameworks[:6]
    databases = stack.databases[:4]
    infra = stack.tools_and_infra[:6]

    lines: list[str] = []
    if languages:
        lines.append("## 主要技术栈（提炼）")
        lines.append(f"- 语言：{', '.join(languages)}")
    if frameworks:
        if not lines:
            lines.append("## 主要技术栈（提炼）")
        lines.append(f"- 框架/中间件：{', '.join(frameworks)}")
    if databases:
        if not lines:
            lines.append("## 主要技术栈（提炼）")
        lines.append(f"- 数据与缓存：{', '.join(databases)}")
    if infra:
        if not lines:
            lines.append("## 主要技术栈（提炼）")
        lines.append(f"- Infra/Components：{', '.join(infra)}")
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
        # 作者默认值：显式 authors → author_identities → email+name。
        # 再对线索做 shortlog 展开，覆盖「Rain」对应多邮箱/多显示名。
        # 防 LLM 误把超短自我介绍当 authors：已有 author_identities 时忽略 len<=3 且无 @ 的传入。
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
        if config is not None and not author_list:
            # 同时收 name + email，避免错误邮箱盖掉可用的名字线索
            for candidate in (config.email, config.name):
                if candidate and candidate not in author_list:
                    author_list.append(candidate)
        if not author_list and config is not None:
            return (
                "未配置作者身份，无法计算你的真实贡献占比。请在 ~/.repo2resume/config.toml 里设 "
                'author_identities = ["邮箱1", "邮箱2"]，或调用时传 authors 参数。'
            )
        # 用名字/邮箱子串在各仓 shortlog 展开成真实身份（解决 config 邮箱与 commit 邮箱不一致）
        try:
            from repo2resume.agent.progress import emit_progress

            emit_progress("解析作者身份并挖掘仓库…")
        except ImportError:  # pragma: no cover
            pass
        expanded = _expand_authors_from_repos(repo_paths, author_list)
        if expanded:
            author_list = expanded
        options = MineOptions(authors=author_list, since=since)
        try:
            stats, profile = run_analyze(
                repo_paths,
                options,
                config=config,
                cache=cache,
                db=db,
                stats_only=stats_only,
                use_cache=True,
            )
        except TimeoutError as exc:
            return (
                f"【分析中断-超时】{exc}\n"
                "已停止；请不要再次调用 analyze_repo / repo_analyst。"
                "可稍后重试或调大 llm_timeout_s。"
            )
        summary = _summarize_stats(stats)
        # 0 贡献时把实际过滤身份写进摘要，方便用户改 config
        if stats.summary.total_author_commits == 0:
            summary += (
                "\nWARNING: author_commits=0；当前过滤身份: "
                + ", ".join(author_list)
                + "。请核对 ~/.repo2resume/config.toml 的 author_identities / email，"
                "或对仓库执行 git shortlog -sne。"
            )
        if not stats_only and profile is not None:
            return (
                "【repo_analyst已完成】请把下方统计与「方向」块原样展示给用户，"
                "请用户确认 primary/secondary 与候选职位类型；"
                "默认仓库已是 ./local_repos/，禁止再问路径；"
                "本轮不要再调 repo_analyst；下一步等用户确认方向后 set_job_prefs。"
                "用户要求重新分析时可再调。\n\n"
                + summary
                + "\n\n"
                + _format_stack_brief(profile)
                + "\n\n"
                + _format_direction_block(profile)
            )
        if not stats_only and profile is None:
            return (
                "【repo_analyst已完成-无画像】"
                + summary
                + "\n\n【画像未生成】LLM 超时或失败；上方统计仍可用。"
                + "请把统计转述给用户；禁止再次调用 analyze_repo / repo_analyst。"
            )
        return summary

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
# Phase 3 tools: job prefs + search + project material retrieval
# ---------------------------------------------------------------------------


class SetJobPrefsParams(BaseModel):
    """set_job_prefs：搜岗前结构化偏好（落库）。"""

    confirmed_directions: list[str] | None = Field(
        default=None,
        description="用户确认后的职位类型，如 ['Python 后端工程师','全栈工程师']；省略则保留已有",
    )
    is_campus: bool | None = Field(
        default=None,
        description="是否校招；不限则 null",
    )
    salary_range: str | None = Field(
        default=None,
        description="期望薪资，如 '20-35k' / '面议'；不限则 null",
    )
    city: str | None = Field(
        default=None,
        description=(
            "目标城市，必须是具体城市名；多个用逗号分隔，如 '北京,上海,广州,深圳'。"
            "用户说北上广深/江浙沪时由你展开，禁止把缩写原样写入。"
            "远程为主时可 null 并设 remote=true"
        ),
    )
    remote: bool | None = Field(
        default=None,
        description="是否接受远程；与 city 至少填一个",
    )
    top_n: int = Field(
        default=5,
        ge=1,
        le=10,
        description="推荐展示条数，默认 5，最多 10",
    )
    include_big_tech: bool = Field(
        default=False,
        description="是否加大厂专项搜索；默认 false，仅用户明确要求时 true",
    )
    big_tech_list: list[str] | None = Field(
        default=None,
        description="覆盖默认大厂名单；仅 include_big_tech=true 时有意义",
    )

    @field_validator("confirmed_directions", "big_tech_list", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _coerce_str_list(value)

    @field_validator("city", "salary_range", mode="before")
    @classmethod
    def _coerce_optional_str(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text or text.lower() in {"null", "none", "不限"}:
                return None
            return text
        return value


def make_set_job_prefs_tool(db: Database | None) -> Tool:
    """工厂：写入 JobSearchPrefs；search_jobs 依赖此表。"""

    def handler(
        confirmed_directions: list[str] | None = None,
        is_campus: bool | None = None,
        salary_range: str | None = None,
        city: str | None = None,
        remote: bool | None = None,
        top_n: int = 5,
        include_big_tech: bool = False,
        big_tech_list: list[str] | None = None,
    ) -> str:
        if db is None:
            return "数据库未就绪，无法保存搜岗偏好。"

        # 与已有偏好合并，避免「只补校招」时把 city/salary 冲掉导致反复 set_job_prefs 卡死
        existing = db.load_job_search_prefs()
        base = existing if isinstance(existing, JobSearchPrefs) else JobSearchPrefs()
        dirs = list(confirmed_directions or [])
        if not dirs:
            dirs = list(base.confirmed_directions)
        # 「全国都行」常见写法：city=全国，并默认接受远程
        city_norm = city if city is not None else base.city
        remote_norm = remote if remote is not None else base.remote
        if city is not None and looks_like_region_alias(city):
            return CITY_ALIAS_MSG.format(city=city.strip())
        if city_norm and str(city_norm).strip() in {"全国", "不限", "都行", "都可以"}:
            if remote_norm is None:
                remote_norm = True
        prefs = JobSearchPrefs(
            confirmed_directions=dirs,
            is_campus=is_campus if is_campus is not None else base.is_campus,
            salary_range=salary_range if salary_range is not None else base.salary_range,
            city=city_norm,
            remote=remote_norm,
            top_n=top_n,
            include_big_tech=include_big_tech or base.include_big_tech,
            big_tech_list=big_tech_list if big_tech_list is not None else base.big_tech_list,
        )
        if existing is not None and top_n == 5 and base.top_n != 5:
            prefs.top_n = base.top_n
        missing = prefs.missing_for_search()
        db.save_job_search_prefs(prefs)
        if missing:
            return (
                "已写入偏好，但仍缺项，暂不可搜岗："
                + ", ".join(missing)
                + "。请向用户补齐后再次 set_job_prefs（会与已有项合并，不必重传全部字段）。"
                "用户说「全国/都行」→ city='全国' 且 remote=true；"
                "用户说北上广深等缩写 → 你展开成具体城市名再写入；"
                "校招社招「都行」→ is_campus=null 即可，不要反复追问。"
            )
        tech_note = (
            f"大厂专项=开（{', '.join(prefs.big_tech_list or DEFAULT_BIG_TECH)}）"
            if prefs.include_big_tech
            else "大厂专项=关（默认）"
        )
        return (
            "【已保存】搜岗偏好就绪。下一步必须调用 job_scout，"
            "不要再调 set_job_prefs，也不要再问校招/城市。\n"
            f"directions={prefs.confirmed_directions}\n"
            f"campus={prefs.is_campus} salary={prefs.salary_range} "
            f"city={prefs.city} remote={prefs.remote}\n"
            f"top_n={prefs.top_n} candidate_budget={CANDIDATE_BUDGET} {tech_note}"
        )

    return Tool(
        name="set_job_prefs",
        description=(
            "在搜岗前写入结构化偏好（方向、城市/远程、薪资、校招、top_n、是否大厂）。"
            "与已有偏好合并，可只传变更字段。用户说全国/都行时设 city='全国' 且 remote=true；"
            "用户说北上广深/江浙沪时展开成具体城市名（北京,上海,广州,深圳），禁止写缩写；"
            "校招社招不限则 is_campus=null。保存成功后应立刻 job_scout，勿反复调用本工具。"
        ),
        params_model=SetJobPrefsParams,
        handler=handler,
        risk="readonly",
    )


class SearchJobsParams(BaseModel):
    """search_jobs 参数。"""

    query: str = Field(
        default="",
        description=("可选覆盖关键词。留空则用 set_job_prefs 的 confirmed_directions。"),
    )
    top_n: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="覆盖 prefs.top_n；省略则用已保存偏好。",
    )
    count: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="兼容旧参数：映射为 top_n（≤10）。",
    )
    source: str = Field(
        default="tavily",
        description=(
            "职位来源：tavily（默认）/ auto（Tavily→猎聘 MCP→Alibaba TOP→mock）/ "
            "liepin_mcp / mock。Bocha 仅显式 source=bocha。"
        ),
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


def _company_label(job: Any) -> str:
    company = getattr(job, "company", None)
    if isinstance(company, str) and company.strip() and company.strip() != "公司未知":
        return company.strip()
    jd = getattr(job, "jd_text", None) or ""
    hit = _COMPANY_HIRE_RE.match(jd.replace("\n", ""))
    if hit:
        return hit.group(1).strip()
    title = str(getattr(job, "title", "") or "")
    titled = _TITLE_COMPANY_RE.search(title)
    if titled:
        return titled.group(1).strip()
    official = official_company_name(getattr(job, "url", None))
    if official:
        return official
    return "公司未知"


def _salary_expect_min_k(salary_range: str | None) -> float | None:
    raw = (salary_range or "").strip().lower()
    if not raw:
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]
    if not nums:
        return None
    if "k" in raw:
        return min(nums)
    # 无单位时按「k」口径处理（用户常写 30 / 30-35）
    return min(nums)


def _salary_line(job: Any, prefs: JobSearchPrefs, analysis_salary: str = "") -> str:
    """期望值一律以 prefs 为准；LLM 文本只在 JD 侧无解析结果时兜底。"""
    blob = f"{getattr(job, 'title', '') or ''} {getattr(job, 'jd_text', '') or ''}"
    info = extract_salary_info(blob)
    shown = (info.raw or "").replace(" ", "")
    expect = (prefs.salary_range or "").strip()
    llm_text = analysis_salary.strip()

    if shown:
        return f"JD {shown}，用户期望 {expect}" if expect else f"JD {shown}"
    if llm_text and llm_text != "JD 未写明":
        return f"{llm_text}（用户期望 {expect}）" if expect else llm_text
    return f"JD 未写明，用户期望 {expect}" if expect else "JD 未写明"


def _apply_job_detail(job: Any, html: str) -> bool:
    """把详情页解析出的字段回填到 Job；返回是否补到了东西。

    搜索摘要经常缺年限和公司，而这些正是硬过滤和打分要用的。详情页正文由
    liveness 判活时顺手带回，这里不发新请求。
    """
    from repo2resume.jobs.detail_parse import parse_detail

    detail = parse_detail(str(getattr(job, "url", "") or ""), html)
    if detail.empty:
        return False

    if detail.company and not (getattr(job, "company", "") or "").strip():
        job.company = detail.company
    if detail.location and not (getattr(job, "location", "") or "").strip():
        job.location = detail.location

    # 年限/学历/薪资拼进 jd_text，_campus_flag 与 extract_salary_info 才看得到
    facts = [
        f"经验要求：{detail.experience}" if detail.experience else "",
        f"学历要求：{detail.education}" if detail.education else "",
        f"薪资：{detail.salary_text}" if detail.salary_text else "",
    ]
    fact_line = "　".join(f for f in facts if f)
    body = detail.jd_text or ""
    merged = "\n".join(p for p in (fact_line, body) if p).strip()
    if merged and len(merged) > len((getattr(job, "jd_text", "") or "").strip()):
        job.jd_text = merged

    if detail.days_since_update is not None:
        job.days_since_update = detail.days_since_update
        job.updated_text = detail.updated_text
    return True


def _dedupe_key(job: Any) -> tuple[str, str]:
    """标题+公司去重键；忽略空白与标点差异。"""

    def _norm(s: str) -> str:
        return re.sub(r"[\s\-_()（）【】\[\]，,。.、/·!！]", "", (s or "").lower())

    return _norm(str(getattr(job, "title", "") or "")), _norm(
        str(getattr(job, "company", "") or "")
    )


def _pref_text_key(text: str) -> str:
    return re.sub(r"[\s，。；、：:;（）()]", "", text or "")


def _pref_clauses(analysis_pref: str, *, drop_city: bool) -> list[str]:
    """把 LLM 的偏好句拆开，丢掉与 prefs 侧重复的分句。

    校招/社招结论一律以 prefs + `_campus_flag` 为准；城市在 JD 已有结构化字段时同理。
    """
    out: list[str] = []
    for raw in re.split(r"[；;。]", analysis_pref or ""):
        clause = raw.strip(" 　,，")
        if not clause:
            continue
        # 按分句主语判断，避免误删「学历要求本科，候选人作为应届可能符合」这种顺带提及
        topic = clause[:5]
        if any(t in topic for t in ("职级", "校招", "社招", "应届")):
            continue
        if drop_city and any(t in topic for t in ("城市", "工作地", "地点")):
            continue
        out.append(clause)
    return out


def _preference_line(job: Any, prefs: JobSearchPrefs, analysis_pref: str = "") -> str:
    """用户侧偏好始终由 prefs 给出；LLM 文本只作为岗位侧的补充说明。"""
    from repo2resume.jobs.matcher import _campus_flag as campus_from_job

    bits: list[str] = []
    loc = (getattr(job, "location", None) or "").strip()
    city = (prefs.city or "").strip()
    if loc and city:
        bits.append(f"工作地 {loc}（用户 {city}）")
    elif loc:
        bits.append(f"工作地 {loc}")
    elif city:
        # JD 结构化字段没有城市时不下「未写清」的断言，让 LLM 那句去补
        bits.append(f"用户 {city}")

    campus_flag = campus_from_job(job)
    if prefs.is_campus is True:
        # 候选池不足时会放宽校招硬门，进榜的社招岗必须自己带上警示
        if campus_flag == "false":
            bits.append("⚠ 官网社招频道/JD 年限，与校招偏好冲突")
        elif campus_flag == "true":
            bits.append("校招，符合偏好")
        else:
            bits.append("用户要校招，JD 未写明")
    elif prefs.is_campus is False:
        if campus_flag == "true":
            bits.append("⚠ JD 指向校招，与社招偏好冲突")
        else:
            bits.append("用户要社招")

    ours = "；".join(bits)
    for clause in _pref_clauses(analysis_pref, drop_city=bool(loc)):
        if _pref_text_key(clause) not in _pref_text_key(ours):
            bits.append(clause)
            ours = "；".join(bits)
    return ours if bits else "请打开 JD 核对城市/职级/学历。"


def _hard_eligible(
    job: Any, prefs: JobSearchPrefs, *, relax_campus: bool = False
) -> tuple[bool, str]:
    """硬约束：明确冲突才淘汰；unknown 不等于 false。

    `relax_campus` 供池子太小时二次放行：国内站点普遍写年限，严格执行校招规则
    会把候选池清空，此时宁可让它们进榜靠 `_campus_pref_score` 排到后面。
    """
    title = str(getattr(job, "title", "") or "")
    jd = str(getattr(job, "jd_text", "") or "")
    loc = str(getattr(job, "location", "") or "")
    blob = f"{title}\n{jd}".lower()

    # 用户无法投递打不开的岗位。mock 只在完全没配联网源时出现，属离线演示，豁免。
    url = str(getattr(job, "url", "") or "").strip()
    if str(getattr(job, "source", "") or "") != "mock" and not url.lower().startswith("http"):
        return False, "无有效职位链接，已过滤"

    if any(tok in blob for tok in ("兼职", "part-time", "part time")):
        return False, "明确兼职，已过滤"

    if str(getattr(job, "source", "") or "") != "mock" and not looks_like_job_title(title):
        return False, "标题不是具体岗位，已过滤"

    from repo2resume.jobs.matcher import _campus_flag as campus_from_job

    if not relax_campus:
        campus_flag = campus_from_job(job)
        if prefs.is_campus is True and campus_flag == "false":
            return False, "与校招偏好冲突（官网社招频道或年限），已过滤"
        if prefs.is_campus is False and campus_flag == "true":
            return False, "与社招偏好冲突，已过滤"

    city = (prefs.city or "").strip()
    if city and city not in {"全国", "不限"}:
        flag = job_city_flag(location=loc, title=title, jd_text=jd, pref_city=city)
        if flag == "false":
            return False, f"城市不匹配（用户 {city}），已过滤"

    expect_min_k = _salary_expect_min_k(prefs.salary_range)
    if expect_min_k is not None:
        info = extract_salary_info(f"{title}\n{jd}")
        if info.known and info.max is not None:
            # 容忍 15% 波动，避免把接近目标的岗位误杀（比如 28k vs 30k）
            threshold = expect_min_k * 0.85
            if info.max < threshold:
                return False, f"薪资上限 {info.max:.0f}K 低于目标 {expect_min_k:.0f}K"
    return True, ""


def _format_job_card(
    *,
    index: int,
    title: str,
    company: str | None,
    site_label: str | None,
    source: str | None,
    overall: Any,
    reason: str,
    url: str,
    snippet: str,
    job_id: str,
    analysis_match: str = "",
    analysis_preference: str = "",
    analysis_salary: str = "",
    analysis_gaps: str = "",
    updated_text: str = "",
) -> str:
    """02 展示契约：Markdown 链接 + 四段分析；不用方括号包 id，避免 Rich markup。"""
    del overall, snippet, source  # 分数与摘要不对用户默认展示
    company_s = (company or "").strip() or "公司未知"
    site_s = (site_label or "").strip() or "未知站点"
    updated_s = (updated_text or "").strip()
    title_s = title.strip() or job_id
    link = (url or "").strip()
    if link.startswith("http"):
        title_line = f"{index}. {title_s}"
        link_line = f"   [职位链接]({link})"
    else:
        title_line = f"{index}. {title_s}"
        link_line = ""
    match_s = analysis_match.strip() or (reason or "").strip() or "与技能画像相关度有限。"
    pref_s = analysis_preference.strip() or "请打开 JD 核对城市/职级/学历。"
    sal_s = analysis_salary.strip() or "JD 未写明"
    gaps_s = analysis_gaps.strip() or "请打开 JD 核对硬性要求与时效。"
    rows = [title_line]
    if link_line:
        rows.append(link_line)
    rows.extend(
        [
            f"   公司：{company_s} · {site_s}" + (f" · {updated_s}" if updated_s else ""),
            f"   匹配点：{match_s}",
            f"   偏好匹配：{pref_s}",
            f"   薪资：{sal_s}",
            f"   缺口：{gaps_s}",
        ]
    )
    lower_link = link.lower()
    if not any(
        token in lower_link for token in ("job_detail", "jobdetail", "/job/", "jobs.zhaopin.com")
    ):
        rows.append("   依据：搜索摘要评分（未打开详情页）")
    rows.append(f"   职位编号：{job_id}")
    return "\n".join(rows)


def _resolve_top_n(prefs: JobSearchPrefs, top_n: int | None, count: int | None) -> int:
    if top_n is not None:
        return max(1, min(10, top_n))
    if count is not None:
        return max(1, min(10, count))
    return max(1, min(10, prefs.top_n))


def _cities_from_pref(city: str | None) -> list[str]:
    """按已保存的具体城市名拆开搜；缩写不在这里展开。"""
    return split_pref_cities(city)


def _preference_query_suffix(prefs: JobSearchPrefs, *, city_override: str | None = None) -> str:
    parts: list[str] = []
    city = (city_override if city_override is not None else prefs.city or "").strip()
    if city and city.lower() not in {"null", "none", "不限", "全国"}:
        parts.append(city)
    elif city in {"全国"} or (prefs.remote is True and not city):
        parts.append("全国")
    if prefs.remote is True:
        parts.append("远程")
    if prefs.is_campus is True:
        parts.append("校招")
    elif prefs.is_campus is False:
        parts.append("社招")
    return " ".join(parts)


def _dedupe_query_tokens(text: str) -> str:
    """去掉重复词，避免「全国 远程 全国 远程」。"""
    seen: set[str] = set()
    out: list[str] = []
    for tok in text.split():
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return " ".join(out)


_DIRECTION_SEPARATORS = re.compile(r"[/／、|｜]|\s+&\s+|\s+and\s+", flags=re.I)
_ROLE_SUFFIXES = ("工程师", "开发", "研发", "架构师", "专家", "engineer", "developer")


def _split_direction(direction: str) -> list[str]:
    """`数据/机器学习工程师` 整串进 query 会命中不到任何岗，拆成两个可搜的方向。

    拆完给丢了职位名词的片段补回后半段的角色后缀（`数据` → `数据工程师`）。
    """
    raw = (direction or "").strip()
    if not raw:
        return []
    parts = [p.strip(" -_") for p in _DIRECTION_SEPARATORS.split(raw)]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [raw]

    lowered_last = parts[-1].lower()
    suffix = next((s for s in _ROLE_SUFFIXES if lowered_last.endswith(s)), "")
    out: list[str] = []
    for part in parts:
        low = part.lower()
        if suffix and not any(s in low for s in _ROLE_SUFFIXES):
            part = f"{part}{suffix}"
        if part not in out:
            out.append(part)
    return out


def _direction_bases(
    prefs: JobSearchPrefs,
    *,
    query_override: str = "",
    limit: int | None = MAX_DIRECTIONS,
) -> list[str]:
    """先截断方向数再拆分。方向太多会把搜索预算摊薄到每个方向都排不出东西。

    截断按原始方向计数，避免把 `数据/机器学习工程师` 这类组合方向拆到一半就砍掉。
    """
    raw = [d.strip() for d in prefs.confirmed_directions if d and d.strip()]
    if not raw and query_override.strip():
        raw = [query_override.strip()]
    if limit is not None:
        raw = raw[:limit]
    bases: list[str] = []
    for direction in raw:
        for part in _split_direction(direction):
            if part not in bases:
                bases.append(part)
    return bases


def _build_search_queries(
    prefs: JobSearchPrefs,
    *,
    query_override: str = "",
    limit: int | None = INITIAL_QUERY_CAP,
    skip: set[str] | None = None,
) -> list[str]:
    """从 prefs 构建搜索词；组合城市拆开搜。薪资只用于打分，不进 query。"""
    bases = _direction_bases(prefs, query_override=query_override)
    cities = _cities_from_pref(prefs.city)
    if not cities:
        cities = [""]
    queries: list[str] = []
    seen: set[str] = set(skip or ())
    for base in bases:
        for city in cities:
            suffix = _preference_query_suffix(prefs, city_override=city or None)
            base_clean = base
            for noise in suffix.split() if suffix else []:
                base_clean = base_clean.replace(noise, " ")
            base_clean = " ".join(base_clean.split()) or base
            q = _dedupe_query_tokens(f"{base_clean} {suffix}".strip())
            if q and q not in seen:
                seen.add(q)
                queries.append(q)
    # 校招或大厂专项：用 site:官网 检索，避免被扩成猎聘/智联
    if (prefs.is_campus is True or prefs.include_big_tech) and bases:
        kind = "校招" if prefs.is_campus is not False else "招聘"
        official_qs = official_site_queries(bases[0], kind=kind, skip=seen)
        seen.update(official_qs)
        cut = min(len(queries), CAMPUS_QUERY_INSERT_AT)
        queries = queries[:cut] + official_qs + queries[cut:]
    if limit is not None:
        return queries[:limit]
    return queries


def _extra_refill_queries(prefs: JobSearchPrefs, used: set[str]) -> list[str]:
    """城市组合用尽后，用招聘/年份变体继续补搜。"""
    bases = _direction_bases(prefs)
    cities = _cities_from_pref(prefs.city) or [""]
    extras: list[str] = []
    seen = set(used)
    campus = "校招" if prefs.is_campus is True else ("社招" if prefs.is_campus is False else "")
    for base in bases:
        for city in cities:
            for extra in ("招聘", "2026", "应届"):
                q = _dedupe_query_tokens(f"{base} {city} {campus} {extra}".strip())
                if q and q not in seen:
                    seen.add(q)
                    extras.append(q)
    if (prefs.is_campus is True or prefs.include_big_tech) and bases:
        kind = "校招" if prefs.is_campus is not False else "招聘"
        extras.extend(official_site_queries(bases[0], kind=kind, skip=seen))
    return extras


def make_search_jobs_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
) -> Tool:
    """工厂：构造 search_jobs 工具。"""
    from repo2resume.jobs.matcher import JobMatcher
    from repo2resume.jobs.search import _counts_toward_quota, search_jobs

    llm = LLMClient(config)
    matcher = JobMatcher(embedder, llm)

    def handler(
        query: str = "",
        top_n: int | None = None,
        count: int | None = None,
        source: str = "tavily",
    ) -> str:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.jobs.liveness import keep_open_jobs
        from repo2resume.jobs.site_rank import site_label, sort_jobs_by_site
        from repo2resume.observability.langsmith_span import span_call

        if db is None:
            return "数据库未就绪，无法搜岗。"
        prefs_obj = db.load_job_search_prefs()
        if prefs_obj is None:
            return PREFS_MISSING_MSG.format(missing="（尚未调用 set_job_prefs）")
        if isinstance(prefs_obj, JobSearchPrefs):
            prefs = prefs_obj
        else:
            prefs = JobSearchPrefs.model_validate(prefs_obj)
        missing = prefs.missing_for_search()
        if missing:
            return PREFS_MISSING_MSG.format(missing=", ".join(missing))
        if looks_like_region_alias(prefs.city):
            return CITY_ALIAS_MSG.format(city=(prefs.city or "").strip())

        requested = (source or "auto").strip().lower() or "auto"

        recommend_n = _resolve_top_n(prefs, top_n, count)
        candidate_budget = CANDIDATE_BUDGET
        used_queries: set[str] = set()
        pending = _build_search_queries(prefs, query_override=query, limit=INITIAL_QUERY_CAP)
        if not pending:
            return PREFS_MISSING_MSG.format(missing="confirmed_directions")

        profile = _load_latest_profile(db)
        emit_progress(
            f"搜岗开始（source={requested}，首轮 {len(pending)} 组关键词，"
            f"搜索预算≈{candidate_budget}，推荐 top_n={recommend_n}）"
        )

        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        live_jobs: list[Any] = []
        n_dead_total = 0
        rounds = 0
        seen_budget = 0

        def _search_batch(batch_queries: list[str]) -> list[Any]:
            n_q = max(len(batch_queries), 1)
            remain = max(1, candidate_budget - seen_budget)
            per_query = max(8, (remain + n_q - 1) // n_q)

            def _search_one(q: str) -> tuple[str, list[Any]]:
                return q, search_jobs(q, count=per_query, config=config, db=db, source=requested)

            found: list[Any] = []
            workers = min(4, len(batch_queries)) or 1
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _search_one, q)
                    for q in batch_queries
                ]
                for fut in as_completed(futures):
                    try:
                        q, batch = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        emit_progress(
                            f"检索失败（已跳过本组）：{type(exc).__name__}: {str(exc)[:80]}"
                        )
                        continue
                    emit_progress(f"联网/本地检索完成：{q}（+{len(batch)}）")
                    for j in batch:
                        url_key = (j.url or "").strip() or j.id
                        if j.id in seen_ids or url_key in seen_urls:
                            continue
                        # 同一岗位常被多城市/多渠道重复挂出，URL 不同但对用户是同一条
                        title_key = _dedupe_key(j)
                        if title_key in seen_titles:
                            continue
                        seen_ids.add(j.id)
                        seen_urls.add(url_key)
                        seen_titles.add(title_key)
                        found.append(j)
            return found

        def _keep_live(batch: list[Any]) -> list[Any]:
            nonlocal n_dead_total
            if not batch:
                return []
            networked = [j for j in batch if j.source != "mock"]
            to_check = networked or batch
            to_check = sort_jobs_by_site(to_check)
            emit_progress(f"核对 {len(to_check)} 条候选是否仍在招…")
            n_enriched = 0

            def _enrich(job: Any, html: str) -> None:
                nonlocal n_enriched
                if _apply_job_detail(job, html):
                    n_enriched += 1

            kept, n_dead = keep_open_jobs(
                to_check,
                on_drop=lambda j, st="offline": emit_progress(
                    f"{'验证页' if st == 'blocked' else '已下线'}，跳过：{(j.title or '')[:40]}"
                ),
                on_detail=_enrich,
            )
            n_dead_total += n_dead
            if n_enriched:
                emit_progress(f"从详情页补全 {n_enriched} 条（公司/年限/薪资/时效/JD）")
            usable = [j for j in kept if _counts_toward_quota(j)]
            dropped_lists = len(kept) - len(usable)
            if n_dead:
                emit_progress(
                    f"已过滤 {n_dead} 条下线/失效（不计入在招池；"
                    f"本轮保留 {len(usable)}，累计在招 {len(live_jobs) + len(usable)}）"
                )
            if dropped_lists:
                emit_progress(f"丢弃搜索列表页 {dropped_lists} 条（不占预算）")
            return usable

        def _enqueue_refill() -> None:
            extra = _build_search_queries(
                prefs, query_override=query, limit=None, skip=used_queries
            )
            for q in extra:
                if q not in used_queries and q not in pending:
                    pending.append(q)
            if not pending:
                for q in _extra_refill_queries(prefs, used_queries):
                    if q not in pending:
                        pending.append(q)

        while rounds < MAX_SEARCH_ROUNDS and seen_budget < candidate_budget:
            if not pending:
                _enqueue_refill()
            if not pending:
                break
            batch_q = pending[:QUERY_BATCH_SIZE]
            pending = pending[QUERY_BATCH_SIZE:]
            used_queries.update(batch_q)
            rounds += 1
            emit_progress(
                f"第 {rounds} 轮搜索 {len(batch_q)} 组关键词"
                f"（在招 {len(live_jobs)} / 预算 {candidate_budget}）…"
            )
            raw = span_call(
                "jobs.recall",
                lambda q=batch_q: _search_batch(q),
                outputs_of=lambda jobs: {"n": len(jobs)},
            )
            kept = span_call(
                "jobs.liveness",
                lambda b=raw: _keep_live(b),
                outputs_of=lambda jobs: {"n_live": len(jobs)},
            )
            seen_budget += len(kept)
            live_jobs.extend(kept)
            if seen_budget < candidate_budget:
                _enqueue_refill()

        if not live_jobs:
            return (
                "【job_scout已完成】未找到仍在招的职位。请检查 Tavily key，换关键词再搜，"
                "或直接粘贴 JD 文本给 generate_resume。"
            )

        candidates = live_jobs[:candidate_budget]
        directions_label = " / ".join(sorted(used_queries)[:6])
        sources = sorted({j.source for j in candidates})
        source_note = "+".join(sources)
        big_note = "大厂专项=开" if prefs.include_big_tech else "大厂专项=关"
        display_header = (
            "【job_scout已完成】请把下列 Markdown **原样**展示给用户"
            "（保留综合 Top-N、四段分析、链接与职位编号；不要改成一段理由）。"
            "本轮不要再调 job_scout。用户说「继续搜/重新搜索」可再调。"
            "用户选序号或职位编号后 generate_resume。"
        )

        def _card(index: int, job: Any, score: Any | None = None) -> str:
            snippet = (job.jd_text or "").replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:160] + "…"
            match = getattr(score, "analysis_match", "") if score is not None else ""
            pref = getattr(score, "analysis_preference", "") if score is not None else ""
            sal = getattr(score, "analysis_salary", "") if score is not None else ""
            gaps = getattr(score, "analysis_gaps", "") if score is not None else ""
            if score is not None:
                reason = getattr(score, "reason", "")
            else:
                reason = "尚未加载技能画像，仅按召回顺序展示。"
            return _format_job_card(
                index=index,
                title=job.title,
                company=_company_label(job),
                site_label=site_label(job.url),
                source=job.source,
                overall=getattr(score, "overall_score", None) if score is not None else None,
                reason=reason,
                url=job.url or "",
                snippet=snippet,
                job_id=job.id,
                analysis_match=match,
                analysis_preference=_preference_line(job, prefs, pref),
                analysis_salary=_salary_line(job, prefs, sal),
                analysis_gaps=gaps,
                updated_text=getattr(job, "updated_text", "") or "",
            )

        def _ask_more(shown: int) -> str:
            if shown < recommend_n:
                return (
                    f"\n当前核实通过 {shown} 个匹配岗位（目标 {recommend_n}），"
                    "已自动补搜仍不足。是否继续搜更多？"
                    "选定后告诉我序号或职位编号。"
                )
            return (
                "\n展示完本轮结果。是否还要继续搜更多岗位"
                "（可换城市、关键词或加大厂）？选定后告诉我序号或职位编号。"
            )

        if profile is None:
            show = candidates[:recommend_n]
            lines = [
                display_header,
                f"找到在招职位（未加载画像；方向：{directions_label}；来源：{source_note}；"
                f"在招 {len(candidates)}，展示 {len(show)}/{recommend_n}；"
                f"{big_note}；下线岗已丢弃不计池）：",
                "",
                f"### Top-{len(show)} 综合匹配",
            ]
            cards = [_card(i, j) for i, j in enumerate(show, start=1)]
            return "\n".join(lines) + "\n\n" + "\n\n".join(cards) + _ask_more(len(show))

        salary_filtered = 0

        def _filter_pool(pool: list[Any], *, relax_campus: bool) -> tuple[list[Any], int]:
            eligible: list[Any] = []
            n_salary = 0
            for j in pool:
                ok, reason = _hard_eligible(j, prefs, relax_campus=relax_campus)
                if ok:
                    eligible.append(j)
                    continue
                if "薪资上限" in reason:
                    n_salary += 1
                if not relax_campus:
                    emit_progress(f"硬过滤：{reason} -> {(getattr(j, 'title', '') or '')[:32]}")
            return eligible, n_salary

        def _score_pool(pool: list[Any]) -> list[Any]:
            nonlocal salary_filtered

            def _do_filter() -> tuple[list[Any], int, bool]:
                eligible, n_salary = _filter_pool(pool, relax_campus=False)
                enforce_campus = True
                if len(eligible) < recommend_n * 2:
                    relaxed, n_relax_salary = _filter_pool(pool, relax_campus=True)
                    if len(relaxed) > len(eligible):
                        emit_progress(
                            f"硬过滤后仅 {len(eligible)} 条，放宽校招/社招硬门至 {len(relaxed)} 条"
                            "（校招偏好改为综合分扣分）"
                        )
                        eligible, n_salary = relaxed, n_relax_salary
                        enforce_campus = False
                emit_progress(
                    f"已召回 {len(pool)} 条在招职位，硬过滤后 {len(eligible)} 条进入匹配打分…"
                )
                return eligible, n_salary, enforce_campus

            eligible, salary_filtered, enforce_campus = span_call(
                "jobs.filter",
                _do_filter,
                outputs_of=lambda t: {
                    "n_eligible": len(t[0]),
                    "n_salary_drop": t[1],
                    "enforce_campus": t[2],
                    "bucket": "business",
                    "stage": "jobs.filter",
                },
            )
            return span_call(
                "jobs.score",
                lambda: matcher.match_all(
                    profile,
                    eligible,
                    top_k=len(eligible),
                    llm_top_n=10,
                    use_llm=True,
                    city=prefs.city,
                    is_campus=prefs.is_campus,
                    salary_range=prefs.salary_range,
                    enforce_campus=enforce_campus,
                ),
                outputs_of=lambda scores: {"n": len(scores)},
            )

        scores = _score_pool(candidates)
        by_id = {j.id: j for j in candidates}

        suitable = [s for s in scores if float(s.overall_score) >= MIN_RECOMMEND_SCORE]

        while (
            len(suitable) < recommend_n
            and rounds < MAX_SEARCH_ROUNDS
            and seen_budget < candidate_budget
        ):
            _enqueue_refill()
            if not pending:
                break
            batch_q = pending[:QUERY_BATCH_SIZE]
            pending = pending[QUERY_BATCH_SIZE:]
            used_queries.update(batch_q)
            rounds += 1
            emit_progress(
                f"匹配岗位不足（匹配 {len(suitable)}/{recommend_n}），第 {rounds} 轮补搜…"
            )
            raw = span_call(
                "jobs.recall",
                lambda q=batch_q: _search_batch(q),
                outputs_of=lambda jobs: {"n": len(jobs)},
            )
            added = span_call(
                "jobs.liveness",
                lambda b=raw: _keep_live(b),
                outputs_of=lambda jobs: {"n_live": len(jobs)},
            )
            seen_budget += len(added)
            if not added:
                continue
            live_jobs.extend(added)
            candidates = live_jobs[:candidate_budget]
            by_id = {j.id: j for j in candidates}
            scores = _score_pool(candidates)
            suitable = [s for s in scores if float(s.overall_score) >= MIN_RECOMMEND_SCORE]

        skill_top = suitable[:recommend_n]
        if len(skill_top) < recommend_n and scores:
            taken = {s.job_id for s in skill_top}
            fillers = [s for s in scores if s.job_id not in taken]
            skill_top = (skill_top + fillers)[:recommend_n]
        number_by_id = {s.job_id: i for i, s in enumerate(skill_top, start=1)}

        shown_n = len(skill_top)
        lines = [
            display_header,
            f"按方向「{directions_label}」推荐 {shown_n} 个职位"
            f"（来源：{source_note}；在招 {len(candidates)}；"
            f"top_n={recommend_n}；搜索预算={candidate_budget}；{big_note}；下线岗已丢弃不计池）：",
        ]
        if sources == ["mock"]:
            lines.append("提示：当前为 mock 示例职位。配置 Tavily key 后重启 chat 再搜。")
        if shown_n < recommend_n:
            lines.append(
                f"注意：目标展示 {recommend_n} 条，当前只核实通过 {shown_n} 条匹配在招岗。"
            )
        if salary_filtered:
            lines.append(f"说明：按你的薪资期望已过滤 {salary_filtered} 条明显低薪岗位。")

        def _cards_for(order: list[Any]) -> list[str]:
            out: list[str] = []
            for s in order:
                job = by_id.get(s.job_id)
                if job is None:
                    continue
                idx = number_by_id.get(s.job_id, len(out) + 1)
                out.append(_card(idx, job, s))
            return out

        skill_cards = _cards_for(skill_top)
        body = (
            "\n".join(lines)
            + f"\n\n### Top-{shown_n} 综合匹配\n\n"
            + "\n\n".join(skill_cards)
            + _ask_more(shown_n)
        )
        return body

    return Tool(
        name="search_jobs",
        description=(
            "基于已保存的 set_job_prefs 搜索并匹配职位。"
            f"无有效偏好会拒搜。最多搜 {MAX_SEARCH_ROUNDS} 轮，"
            f"攒够 {CANDIDATE_BUDGET} 条在招职位即停（下线岗与无链接岗不计），返回 top_n 条。"
            f"最多只搜前 {MAX_DIRECTIONS} 个方向，避免预算摊薄。"
            "默认 source=tavily；auto 会尝试 Tavily→猎聘 MCP→Alibaba TOP；"
            "未配置任何联网 key 时才降级 mock 示例职位。"
        ),
        params_model=SearchJobsParams,
        handler=handler,
        risk="network",
    )


class JobScoutParams(BaseModel):
    task: str = Field(
        default="",
        description="搜岗任务说明；可留空，按已保存偏好搜索。",
    )


def make_job_scout_direct_tool(
    search_jobs_tool: Tool,
    *,
    tool_hooks: list | None = None,
) -> Tool:
    """chat 用的 job_scout：不再套一层「选工具」LLM，直接 search_jobs(source=auto)。

    子 loop 在 glm-4.5-air 上经常几十秒还不发 search_jobs，主 loop 连打三次
    触发卡死，界面只有「搜岗匹配中」没有【job_scout已完成】。
    """
    from repo2resume.agent.tools import ToolRegistry

    def handler(task: str = "") -> str:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.observability.langsmith_span import span_call

        emit_progress("子代理 job_scout 调用工具：search_jobs")
        inner = ToolRegistry()
        inner.register(search_jobs_tool)
        for hook in tool_hooks or []:
            inner.add_hook(hook)

        def _run() -> str:
            return str(inner.call("search_jobs", {"query": "", "source": "auto"}))

        return span_call(
            "subagent.job_scout",
            _run,
            run_type="chain",
            inputs={"task": (task or "")[:400], "passthrough": True},
            outputs_of=lambda text: {"preview": text or ""},
        )

    return Tool(
        name="job_scout",
        description=(
            "委派给职位搜索专家：按已保存偏好搜岗并返回匹配列表。"
            "拿到【job_scout已完成】后先向用户展示结果；用户要求重新搜索时可再调用。"
            "禁止声称「每人只能搜一次」。"
        ),
        params_model=JobScoutParams,
        handler=handler,
        risk="readonly",
    )


def _job_scout_finished(text: str) -> bool:
    return "【job_scout已完成】" in text or "【需先 set_job_prefs】" in text


def make_job_scout_subagent_tool(
    search_jobs_tool: Tool,
    find_materials_tool: Tool | None = None,
    *,
    llm: Any,
    tool_hooks: list | None = None,
) -> Tool:
    """chat 用的 job_scout：SubAgent-as-Tool（独立 loop）+ 未完成时直调 search_jobs。"""
    from repo2resume.agent.subagent import SubAgentRunner, make_job_scout_spec

    hooks = list(tool_hooks or [])
    runner = SubAgentRunner(
        make_job_scout_spec(search_jobs_tool, find_materials_tool),
        llm=llm,
        tool_hooks=hooks,
    )
    sub = runner.as_tool()
    direct = make_job_scout_direct_tool(search_jobs_tool, tool_hooks=hooks)
    default_task = "按已保存偏好搜索职位。第一轮必须调用 search_jobs，query 留空，source=auto。"

    def handler(task: str = "") -> str:
        from repo2resume.agent.progress import emit_progress

        prompt = (task or "").strip() or default_task
        out = str(sub.call({"task": prompt}))
        if _job_scout_finished(out):
            return out
        emit_progress("job_scout 子代理未完成搜岗，回退直调 search_jobs")
        return str(direct.call({"task": prompt}))

    return Tool(
        name="job_scout",
        description=(
            "委派给职位搜索专家：按已保存偏好搜岗并返回匹配列表。"
            "拿到【job_scout已完成】后先向用户展示结果；用户要求重新搜索时可再调用。"
            "禁止声称「每人只能搜一次」。"
        ),
        params_model=JobScoutParams,
        handler=handler,
        risk="readonly",
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


def make_find_project_materials_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
) -> Tool:
    """工厂：构造 find_project_materials 工具。"""
    from repo2resume.resume.pipeline import apply_resume_rerank, normalize_resume_reranker
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.store import VectorStore

    store = VectorStore(db, embedder) if db else None

    def handler(query: str = "", top_k: int = 5) -> str:
        if not query.strip():
            return "请提供 query 参数，例如目标职位 JD 中的关键职责。"
        if store is None:
            return "数据库未就绪，无法检索项目素材。"
        profile = _load_latest_profile(db)
        if profile is None:
            return "尚未生成技能画像，请先运行 analyze 或在 chat 中说「分析我的项目」。"

        # 优先带上 analyze 落盘的完整 stats（README / commits / deps）；无则仅画像摘要。
        stats = db.load_latest_repo_stats_bundle() if db is not None else None
        store.upsert_profile_materials(profile, stats)

        hits = hybrid_search(store, query, top_k=top_k * 2)
        if not hits:
            return "未找到与查询相关的项目素材。"
        mode = normalize_resume_reranker(config.resume_reranker)
        if mode == "off":
            ranked = hits[:top_k]
        else:
            ranked, _mode = apply_resume_rerank(
                hits,
                query,
                mode=mode,
                llm=LLMClient(config),
                top_k=top_k,
            )
        lines = [f"针对「{query}」召回 top-{len(ranked)} 项目素材："]
        for h in ranked:
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
        default=True,
        description="True=只导出项目经历块（推荐）；False=含姓名/技能摘要的完整稿。",
    )


def make_generate_resume_tool(
    config: AppConfig,
    db: Database | None,
    embedder: Any,
    llm: LLMClient | None = None,
) -> Tool:
    """工厂：检索 + Writer-Critic + 渲染落盘。"""
    from repo2resume.resume.pipeline import run_resume_pipeline

    def handler(
        jd_text: str = "",
        job_id: str | None = None,
        output_path: str = "resume_draft.md",
        projects_only: bool = True,
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
        client = llm if llm is not None else LLMClient(config)
        try:
            result = run_resume_pipeline(
                config=config,
                db=db,
                embedder=embedder,
                llm=client,
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
                    "提示：job_id 须为 search_jobs 返回的内部 id（如 tavily-xxxx），"
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
        critic_complete = result.reports[-1].critic_complete if result.reports else False
        preview = result.markdown[:1200]
        path = result.output_path or Path(output_path)
        from repo2resume.resume.critic import format_generate_resume_status

        status = format_generate_resume_status(result.reports)
        return (
            f"【已完成】简历草稿已写入，勿再调用本工具。\n"
            f"draft_id={result.draft_id} → {path}\n"
            f"Critic 轮次={len(result.reports)} approved={approved} "
            f"critic_complete={critic_complete} 末轮 must={must_left}\n"
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
