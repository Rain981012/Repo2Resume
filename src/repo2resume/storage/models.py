"""Pydantic models for analysis stats and skill profiles."""

from __future__ import annotations

from typing import Any

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
    lines_added: int = 0
    files_touched: int = 0
    share: float | None = None


class ProjectSummary(BaseModel):
    """Per-repo mining result (machine stats)."""

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
    repo_count: int = 0
    total_author_commits: int = 0
    overall_language_share: dict[str, float] = Field(default_factory=dict)


class RepoStatsBundle(BaseModel):
    """Multi-repo stats document (Phase A stats.json shape)."""

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
                item.get("reason")
                or item.get("message")
                or item.get("text")
                or item.get("caution")
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
    entries: list[FactEntry] = Field(default_factory=list)

    def as_prompt_block(self) -> str:
        lines = []
        for e in self.entries:
            lines.append(f"- {e.key}: {e.value!r} {e.evidence.render()}")
        return "\n".join(lines)
