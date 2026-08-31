"""【AI 辅助】analyze_repo 工具单测：mock 掉 mine，验证 handler 返回摘要。"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo2resume.agent.builtins import make_analyze_tool
from repo2resume.storage.models import (
    ProjectSummary,
    RepoStatsBundle,
    SkillProfile,
    StatsSummary,
    TechStack,
)


@pytest.fixture()
def fake_bundle(monkeypatch):
    bundle = RepoStatsBundle(
        generated_at="2026-01-01T00:00:00+00:00",
        author_filters=["ada@example.com"],
        summary=StatsSummary(
            repo_count=1,
            total_author_commits=5,
            overall_language_share={"Python": 0.8, "TypeScript": 0.2},
        ),
        repos=[
            ProjectSummary(
                name="demo",
                path="/tmp/demo",
                head_commit="abc",
                total_commits=20,
                author_commits=5,
                author_share=0.25,
                dependencies={"requirements.txt": ["fastapi"]},
            )
        ],
    )

    def fake_run_analyze(paths, options, *, config, cache, db, stats_only, use_cache):
        return bundle, None

    monkeypatch.setattr("repo2resume.agent.builtins.run_analyze", fake_run_analyze)
    return bundle


def test_analyze_tool_schema_has_fields() -> None:
    tool = make_analyze_tool(config=None, cache=None, db=None)
    schema = tool.json_schema()
    props = schema["function"]["parameters"]["properties"]
    assert {"paths", "authors", "since", "stats_only"} <= set(props)


def test_analyze_tool_returns_summary(fake_bundle) -> None:
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({"paths": ["/tmp/demo"], "authors": ["ada@example.com"]})
    assert "repo_count: 1" in out
    assert "total_author_commits: 5" in out
    assert "Python 80.0%" in out
    assert "demo: commits=5/20" in out
    assert "deps=requirements.txt" in out
    # share 0.25 >= 0.15 → no caution marker
    assert "low_author_share]" not in out


def test_analyze_tool_marks_low_share(fake_bundle) -> None:
    # 改成低贡献仓
    fake_bundle.repos[0].author_share = 0.04
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({"paths": ["/tmp/demo"]})
    assert "low_author_share]" in out


def test_analyze_tool_empty_paths_uses_discovery(fake_bundle, monkeypatch) -> None:
    """paths 为空 → 走 _discover_local_repos，仍调到 run_analyze。"""
    monkeypatch.setattr(
        "repo2resume.agent.builtins._discover_local_repos",
        lambda base=None: [Path("/tmp/demo")],
    )
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({})  # 不传 paths
    assert "repo_count: 1" in out


def test_analyze_tool_no_repos_found(monkeypatch) -> None:
    """paths 空 + 扫不到仓库 → 返回提示，不调 run_analyze。"""
    monkeypatch.setattr("repo2resume.agent.builtins._discover_local_repos", lambda base=None: [])
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({})
    assert "未找到仓库" in out


def test_analyze_tool_coerces_authors_json_string(fake_bundle) -> None:
    """LLM 常把 authors 传成 '["Rain"]' 字符串；应校验通过并跑通。"""
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({"paths": ["/tmp/demo"], "authors": '["Rain"]'})
    assert "repo_count: 1" in out


def test_analyze_tool_coerces_paths_json_string(fake_bundle) -> None:
    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({"paths": '["/tmp/demo"]', "authors": ["ada@example.com"]})
    assert "repo_count: 1" in out


def test_analyze_tool_includes_stack_brief_when_profile_present(monkeypatch) -> None:
    bundle = RepoStatsBundle(
        generated_at="2026-01-01T00:00:00+00:00",
        summary=StatsSummary(
            repo_count=1,
            total_author_commits=8,
            overall_language_share={"Python": 1.0},
        ),
        repos=[
            ProjectSummary(
                name="demo",
                path="/tmp/demo",
                head_commit="abc",
                total_commits=8,
                author_commits=8,
                author_share=1.0,
            )
        ],
    )
    profile = SkillProfile(
        primary_direction="Python 后端",
        secondary_directions=["数据"],
        tech_stack=TechStack(
            languages=["Python", "TypeScript"],
            frameworks=["FastAPI", "Celery"],
            databases=["Redis", "PostgreSQL"],
            tools_and_infra=["Nginx", "Docker", "GitHub Actions"],
        ),
    )

    def fake_run_analyze(paths, options, *, config, cache, db, stats_only, use_cache):
        return bundle, profile

    monkeypatch.setattr("repo2resume.agent.builtins.run_analyze", fake_run_analyze)

    tool = make_analyze_tool(config=None, cache=None, db=None)
    out = tool.call({"paths": ["/tmp/demo"], "authors": ["ada@example.com"]})
    assert "主要技术栈（提炼）" in out
    assert "框架/中间件：FastAPI, Celery" in out
    assert "数据与缓存：Redis, PostgreSQL" in out
    assert "Infra/Components：Nginx, Docker, GitHub Actions" in out
