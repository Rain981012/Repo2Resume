from __future__ import annotations

from pathlib import Path

from repo2resume.analysis.git_miner import MineOptions, mine_one
from repo2resume.analysis.tech_detector import collect_dependencies, detect_tech_stack
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


def test_collect_dependencies_supports_ops_files(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir(parents=True)
    (repo / "requirements").mkdir()
    (repo / "requirements" / "dev.txt").write_text("celery==5.3.0\nredis>=4.0\n")
    (repo / "docker-compose.dev.yml").write_text(
        """
services:
  worker:
    image: celery:5
    depends_on:
      - redis
  proxy:
    image: nginx:1.25
"""
    )
    (repo / "nginx.prod.conf").write_text(
        """
events {}
http {
  upstream backend { server app:8000; }
  server { listen 80; proxy_pass http://backend; }
}
"""
    )

    deps = collect_dependencies(repo)
    assert "requirements/dev.txt" in deps
    assert "celery" in [d.lower() for d in deps["requirements/dev.txt"]]
    assert "docker-compose.dev.yml" in deps
    assert any("nginx" in d.lower() for d in deps["docker-compose.dev.yml"])
    assert "nginx.prod.conf" in deps
    assert "nginx" in [d.lower() for d in deps["nginx.prod.conf"]]
