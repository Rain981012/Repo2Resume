from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from repo2resume.cli import app
from repo2resume.config import load_config
from tests.helpers_git import init_fixture_repo

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_init_non_interactive(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPO2RESUME_LLM_API_KEY", "from-env")
    result = runner.invoke(app, ["init", "--non-interactive"])
    assert result.exit_code == 0
    cfg = load_config(data_dir)
    assert cfg.llm_api_key == "from-env"
    assert cfg.config_path.is_file()
    assert cfg.db_path.is_file()


def test_analyze_stats_only(data_dir: Path, tmp_path: Path) -> None:
    _ = data_dir
    repo = init_fixture_repo(tmp_path / "demo")
    result = runner.invoke(
        app,
        [
            "analyze",
            str(repo),
            "--author",
            "ada@example.com",
            "--stats-only",
            "--no-cache",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "demo" in result.stdout
    assert "author_commits" in result.stdout


def test_analyze_defaults_to_local_repos(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = data_dir
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "local_repos"
    init_fixture_repo(local / "demo")
    # Interactive pick: choose Ada (#2 is often Other if sorted by commits — Ada has 2)
    result = runner.invoke(
        app,
        ["analyze", "--stats-only", "--no-cache"],
        input="1\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert (
        "Authors found" in out or "Only one author" in out or "Selected" in out
    )
    assert "author_commits" in out


def test_analyze_interactive_selects_author(
    data_dir: Path, tmp_path: Path
) -> None:
    _ = data_dir
    repo = init_fixture_repo(tmp_path / "demo")
    result = runner.invoke(
        app,
        ["analyze", str(repo), "--stats-only", "--no-cache"],
        input="1\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "author_commits" in result.stdout


def test_analyze_missing_local_repos_errors(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = data_dir
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["analyze", "--stats-only"])
    assert result.exit_code == 1
    assert "local_repos" in result.stdout
