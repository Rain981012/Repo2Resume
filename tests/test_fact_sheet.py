"""【手写】TDD：实现 build_fact_sheet 后，去掉下面的 skip。"""

from __future__ import annotations

import pytest

from repo2resume.analysis.fact_sheet import build_fact_sheet
from repo2resume.storage.models import (
    LanguageStat,
    ProjectSummary,
    RepoStatsBundle,
    StatsSummary,
)


def _sample_stats() -> RepoStatsBundle:
    return RepoStatsBundle(
        generated_at="2026-01-01T00:00:00+00:00",
        author_filters=["ada@example.com"],
        summary=StatsSummary(
            repo_count=2,
            total_author_commits=10,
            overall_language_share={"Python": 0.7, "TypeScript": 0.3},
        ),
        repos=[
            ProjectSummary(
                name="api",
                path="/tmp/api",
                head_commit="abc",
                total_commits=20,
                author_commits=12,
                author_share=0.6,
                languages={"Python": LanguageStat(lines_added=100, files_touched=3, share=1.0)},
                dependencies={"requirements.txt": ["fastapi"]},
            ),
            ProjectSummary(
                name="tiny",
                path="/tmp/tiny",
                head_commit="def",
                total_commits=50,
                author_commits=2,
                author_share=0.04,
                languages={},
            ),
        ],
    )


def test_build_fact_sheet_covers_languages_and_repos() -> None:
    sheet = build_fact_sheet(_sample_stats())
    keys = {e.key for e in sheet.entries}

    assert "summary.overall_language_share.Python" in keys
    assert "summary.overall_language_share.TypeScript" in keys

    py = next(e for e in sheet.entries if e.key.endswith(".Python"))
    assert py.value == 0.7
    assert py.evidence.source == "summary.overall_language_share"

    assert "api.author_commits" in keys
    assert "api.author_share" in keys
    assert "api.dependencies" in keys
    assert "tiny.low_author_share" in keys


def test_authorship_hints_and_fact_sheet_from_profile() -> None:
    from repo2resume.analysis.fact_sheet import (
        authorship_hints_from_profile,
        fact_sheet_from_profile,
    )
    from repo2resume.storage.models import ProjectOneLiner, SkillProfile

    profile = SkillProfile(
        primary_direction="Python",
        project_one_liners=[
            ProjectOneLiner(repo="NLP_GAME", summary="game"),
            ProjectOneLiner(repo="socialdistribution", summary="social"),
        ],
        caution=[
            "NLP_GAME: author_share=0.111 < 0.15，勿写成个人主导",
            "socialdistribution: author_share=0.137 < 0.15，commits=54/393，勿写成个人主导",
        ],
    )
    hints = authorship_hints_from_profile(profile)
    assert hints["NLP_GAME"] == pytest.approx(0.111)
    assert hints["socialdistribution"] == pytest.approx(0.137)
    keys = {e.key for e in fact_sheet_from_profile(profile).entries}
    assert "NLP_GAME.low_author_share" in keys
    assert "socialdistribution.author_share" in keys
