from __future__ import annotations

from pathlib import Path

from repo2resume.analysis.git_miner import collect_authors
from tests.helpers_git import init_fixture_repo


def test_collect_authors(tmp_path: Path) -> None:
    repo = init_fixture_repo(tmp_path / "demo")
    authors = collect_authors([repo])
    emails = {a.email for a in authors}
    assert "ada@example.com" in emails
    assert "other@example.com" in emails
    ada = next(a for a in authors if a.email == "ada@example.com")
    assert ada.commits == 2
