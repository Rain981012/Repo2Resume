"""Pydantic 数据模型：分析统计 + 技能画像 + 事实清单的统一 schema。

这些模型既是运行时的数据载体，也是 LLM 输出的校验 schema（`model_validate_json` 直接
验 LLM 返回的 JSON）。`EvidenceRef` / `FactEntry` / `FactSheet` 是 Phase 1 反幻觉机制的核心——
每条声明都带可溯源的证据指针。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EvidenceRef(BaseModel):
    """Structured evidence pointer (repo / field / optional path detail)."""

    source: str
    detail: str | None = None

    @classmethod
    def from_any(cls, value: Any) -> EvidenceRef:
        if isinstance(value, EvidenceRef):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        if isinstance(value, str):
            return cls(source=value)
        raise TypeError(f"Cannot coerce EvidenceRef from {type(value)!r}")

    def render(self) -> str:
        if self.detail:
            return f"(来源: {self.source}; {self.detail})"
        return f"(来源: {self.source})"


def _coerce_evidence(value: Any) -> Any:
    if value is None or isinstance(value, EvidenceRef):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"source": value}
    return value


class LanguageStat(BaseModel):
    """单语言统计：新增行数、触及文件数、占代码总行数份额（非代码语言为 None）。"""

    lines_added: int = 0
    files_touched: int = 0
    share: float | None = None


class ProjectSummary(BaseModel):
    """单仓库挖矿结果（机器统计），是 `RepoStatsBundle.repos` 的元素类型。"""

    name: str
    path: str
    head_commit: str
    total_commits: int
    author_commits: int
    author_share: float
    contributors: int = 0
    remote: str | None = None
    languages: dict[str, LanguageStat] = Field(default_factory=dict)
    active_from: str | None = None
    active_to: str | None = None
    active_months: int = 0
    monthly_commits: dict[str, int] = Field(default_factory=dict)
    commit_types: dict[str, int] = Field(default_factory=dict)
    top_directories: dict[str, int] = Field(default_factory=dict)
    recent_commit_subjects: list[str] = Field(default_factory=list)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    readme_excerpt: str | None = None
    warning: str | None = None


class StatsSummary(BaseModel):
    """跨仓库汇总：仓库数、作者总 commit 数、按行数加权的全局语言份额。"""

    repo_count: int = 0
    total_author_commits: int = 0
    overall_language_share: dict[str, float] = Field(default_factory=dict)


class RepoStatsBundle(BaseModel):
    """多仓库统计文档（Phase A stats.json 的形状），是分析流水线的核心数据载体。"""

    generated_at: str
    author_filters: list[str] = Field(default_factory=list)
    since: str | None = None
    summary: StatsSummary = Field(default_factory=StatsSummary)
    repos: list[ProjectSummary] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)


class LanguageShare(BaseModel):
    name: str
    share: float
    evidence: EvidenceRef

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> Any:
        return _coerce_evidence(value)


class DomainTag(BaseModel):
    name: str
    evidence: EvidenceRef

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> Any:
        return _coerce_evidence(value)


class TechStack(BaseModel):
    """技术栈分类：语言 / 框架 / 数据库 / 工具与基础设施 / 其他。"""

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    tools_and_infra: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class Highlight(BaseModel):
    repo: str
    claim: str
    evidence: EvidenceRef

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> Any:
        return _coerce_evidence(value)


class ProjectOneLiner(BaseModel):
    repo: str
    summary: str


class JobDirection(BaseModel):
    title: str
    reason: str


class SkillProfile(BaseModel):
    """技能画像：LLM 生成、Pydantic 校验的最终产物，供简历生成与职位匹配消费。"""

    primary_direction: str
    secondary_directions: list[str] = Field(default_factory=list)
    coding_language: list[LanguageShare] = Field(default_factory=list)
    domains: list[DomainTag] = Field(default_factory=list)
    tech_stack: TechStack = Field(default_factory=TechStack)
    highlights_pool: list[Highlight] = Field(default_factory=list)
    project_one_liners: list[ProjectOneLiner] = Field(default_factory=list)
    caution: list[str] = Field(default_factory=list)
    job_direction_suggestions: list[JobDirection] = Field(default_factory=list)
    source_stats_hash: str | None = None
    created_at: str | None = None

    @staticmethod
    def _coerce_caution_item(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            repo = item.get("repo") or item.get("name")
            reason = (
                item.get("reason") or item.get("message") or item.get("text") or item.get("caution")
            )
            if repo and reason:
                return f"{repo}: {reason}"
            if reason:
                return str(reason)
            if repo:
                return str(repo)
            return str(item)
        return str(item)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "gaps_or_cautions" in data and "caution" not in data:
            data = {**data, "caution": data["gaps_or_cautions"]}
        stack = data.get("tech_stack")
        if isinstance(stack, list):
            data = {**data, "tech_stack": {"other": stack}}
        caution = data.get("caution")
        if isinstance(caution, list):
            data = {
                **data,
                "caution": [cls._coerce_caution_item(item) for item in caution],
            }
        return data


class FactEntry(BaseModel):
    """One grounded fact used to constrain LLM claims. (Phase 1 手写核心)"""

    key: str
    value: Any
    evidence: EvidenceRef

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> Any:
        return _coerce_evidence(value)


class FactSheet(BaseModel):
    """事实清单：一组带证据的 `FactEntry`，渲染成 prompt block 约束 LLM 不幻觉。"""

    entries: list[FactEntry] = Field(default_factory=list)

    def as_prompt_block(self) -> str:
        """把所有事实条目渲染成 `- key: value (来源: source)` 的多行文本，塞进 LLM prompt。"""
        lines = []
        for e in self.entries:
            lines.append(f"- {e.key}: {e.value!r} {e.evidence.render()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 3: retrieval + jobs 数据模型
# ---------------------------------------------------------------------------


class DocumentChunk(BaseModel):
    """存入向量库与全文索引的一个可检索片段。"""

    doc_id: str
    text: str
    repo: str | None = None
    chunk_type: str = "summary"  # summary / highlight / readme / tech_stack
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    """单次检索（向量或关键词）返回的命中项。"""

    doc_id: str
    text: str
    score: float
    rank: int
    source: str  # "vector" or "keyword"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    """职位条目：来自网络搜索或手动输入。"""

    id: str
    title: str
    company: str | None = None
    location: str | None = None
    jd_text: str = ""
    skills: list[str] = Field(default_factory=list)
    source: str = "mock"  # mock / tavily / manual
    url: str | None = None
    posted_at: str | None = None


class MatchScore(BaseModel):
    """职位与画像的匹配分数。"""

    job_id: str
    overall_score: float  # 0-1
    vector_score: float | None = None
    llm_score: float | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Resume drafts (Phase 4 Writer / Critic)
# ---------------------------------------------------------------------------


class BulletDraft(BaseModel):
    """一条可溯源的项目经历 bullet（结构化；渲染时再拼 Markdown）。"""

    label: str
    body: str
    evidence: EvidenceRef

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> Any:
        return _coerce_evidence(value)


class ProjectExperienceDraft(BaseModel):
    """单个项目的经历草稿：名 + 一句话产品定位 + bullets。"""

    project_name: str
    one_liner: str
    bullets: list[BulletDraft] = Field(default_factory=list)
    rank: int = 0


class ResumeDraft(BaseModel):
    """MVP：仅项目经历块（非完整简历）。供 Writer 产出、Critic 审阅。"""

    locale: str = "zh-CN"
    job_id: str | None = None
    job_title: str | None = None
    projects: list[ProjectExperienceDraft] = Field(default_factory=list)
    materials_used: list[str] = Field(default_factory=list)


class CritiqueItem(BaseModel):
    """单条审稿意见：只指出问题与改法方向，不重写全文。"""

    severity: Literal["must", "should"]
    category: Literal["fact", "ats", "style", "structure"]
    target: str
    message: str


class CritiqueReport(BaseModel):
    """一次 Critic 审阅结果。approved = must_fix 为空。"""

    must_fix: list[CritiqueItem] = Field(default_factory=list)
    should_fix: list[CritiqueItem] = Field(default_factory=list)
    passed: list[str] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return not self.must_fix
