"""Built-in agent tools — 把 Phase 1 的分析能力包装成 Tool 注册进 loop。

【AI 辅助】模块 — 契约（Tool 抽象）已由 agent/tools.py 定好，这里只填实现。
关键设计：运行时依赖（config/cache/db）通过工厂函数闭包注入，不进 params schema。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from repo2resume.agent.tools import Tool
from repo2resume.analysis.git_miner import MineOptions
from repo2resume.analysis.pipeline import run_analyze
from repo2resume.config import AppConfig
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database


class AnalyzeRepoParams(BaseModel):
    """analyze_repo 的参数（给 LLM 看的 schema）。"""

    paths: list[str] = Field(
        default_factory=list,
        description="本地 git 仓库路径列表；为空则自动扫描当前目录下的 ./local_repos/",
    )
    authors: list[str] | None = Field(
        None, description="按邮箱或名字子串过滤作者；为空则不过滤"
    )
    since: str | None = Field(
        None,
        description="只统计此日期之后的提交，YYYY-MM-DD；不需要时省略或填 null，不要填字符串 'null'",
    )
    stats_only: bool = Field(
        True,
        description="True=只返回 git 统计摘要（省 LLM 成本）；False=同时生成技能画像",
    )


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


def _summarize_stats(stats: Any) -> str:
    """把 RepoStatsBundle 压成给 LLM 看的紧凑摘要（省 token）。"""
    lines: list[str] = []
    s = stats.summary
    lines.append(f"repo_count: {s.repo_count}")
    lines.append(f"total_author_commits: {s.total_author_commits}")

    lang_items = sorted(s.overall_language_share.items(), key=lambda kv: -kv[1])
    lang_str = ", ".join(f"{l} {v:.1%}" for l, v in lang_items[:6])
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
        stats_only: bool = True,
    ) -> str:
        repo_paths = [Path(p).expanduser() for p in (paths or [])]
        if not repo_paths:
            repo_paths = _discover_local_repos()
        if not repo_paths:
            return "未找到仓库：请提供 paths，或在当前目录下建 ./local_repos/ 放 git 仓库。"
        # 防御性清洗：模型常把「无值」填成字符串 "null" 或空串，统一当成 None
        if since and since.strip().lower() in {"null", "none", ""}:
            since = None
        options = MineOptions(authors=list(authors or []), since=since)
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
            "分析本地 git 仓库，返回按作者过滤的提交统计摘要"
            "（语言占比、每仓贡献量、低贡献 caution）。paths 为空时自动扫描 ./local_repos/。"
            "用于回答「我在某仓库做了什么」。"
        ),
        params_model=AnalyzeRepoParams,
        handler=handler,
    )
