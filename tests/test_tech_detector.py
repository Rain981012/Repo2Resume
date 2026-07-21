from __future__ import annotations

from pathlib import Path

from repo2resume.analysis.git_miner import MineOptions, mine_one
from repo2resume.analysis.tech_detector import detect_tech_stack
from repo2resume.storage.models import RepoStatsBundle, StatsSummary
from tests.helpers_git import init_fixture_repo


def test_detect_tech_stack_classifies_fastapi(tmp_path: Path) -> None:
    repo = init_fixture_repo(tmp_path / "demo")
    project = mine_one(repo, MineOptions(authors=["ada@example.com"]))
    bundle = RepoStatsBundle(
        generated_at="t",
        summary=StatsSummary(
            repo_count=1,
            total_author_commits=project.author_commits,
            overall_language_share={"Python": 1.0},
        ),
        repos=[project],
    )
    stack = detect_tech_stack(bundle)
    assert "Python" in stack.languages
    assert any("fastapi" in x.lower() for x in stack.frameworks)
