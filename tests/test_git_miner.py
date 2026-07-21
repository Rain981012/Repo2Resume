from __future__ import annotations

from pathlib import Path

from repo2resume.analysis.git_miner import MineOptions, mine_one, mine_repos
from tests.helpers_git import init_fixture_repo


def test_mine_one_author_filter(tmp_path: Path) -> None:
    repo = init_fixture_repo(tmp_path / "demo")
    project = mine_one(repo, MineOptions(authors=["ada@example.com"]))

    assert project.name == "demo"
    assert project.total_commits == 3
    assert project.author_commits == 2
    assert project.author_share == round(2 / 3, 3)
    assert "Python" in project.languages
    assert project.languages["Python"].lines_added > 0
    assert "fastapi" in project.dependencies.get("requirements.txt", [])
    assert project.warning is None


def test_mine_one_author_miss(tmp_path: Path) -> None:
    repo = init_fixture_repo(tmp_path / "demo")
    project = mine_one(repo, MineOptions(authors=["nobody@example.com"]))
    assert project.author_commits == 0
    assert project.warning is not None


def test_mine_repos_bundle(tmp_path: Path) -> None:
    a = init_fixture_repo(tmp_path / "a")
    b = init_fixture_repo(tmp_path / "b")
    bundle = mine_repos(
        [a, b],
        MineOptions(authors=["Ada Lovelace"]),
    )
    assert bundle.summary.repo_count == 2
    assert bundle.summary.total_author_commits == 4
    assert "Python" in bundle.summary.overall_language_share
