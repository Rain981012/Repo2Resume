"""Rule-based tech stack detection from manifests + language stats."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

from repo2resume.analysis.lang_map import NON_CODE_LANGS
from repo2resume.storage.models import ProjectSummary, RepoStatsBundle, StatsSummary, TechStack

MANIFEST_PARSERS: dict[str, Callable[[Path], list[str]]] = {}


def manifest(filename: str):
    def deco(fn: Callable[[Path], list[str]]) -> Callable[[Path], list[str]]:
        MANIFEST_PARSERS[filename] = fn
        return fn

    return deco


@manifest("requirements.txt")
def _parse_requirements(p: Path) -> list[str]:
    deps = []
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            deps.append(re.split(r"[<>=!~\[; ]", line)[0])
    return deps


@manifest("pyproject.toml")
def _parse_pyproject(p: Path) -> list[str]:
    try:
        data = tomllib.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    deps = list(data.get("project", {}).get("dependencies", []))
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    deps += [k for k in poetry if k.lower() != "python"]
    return [re.split(r"[<>=!~\[; ]", d)[0] for d in deps]


@manifest("package.json")
def _parse_package_json(p: Path) -> list[str]:
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return list(data.get("dependencies", {})) + list(data.get("devDependencies", {}))


@manifest("go.mod")
def _parse_go_mod(p: Path) -> list[str]:
    deps = []
    for line in p.read_text(errors="ignore").splitlines():
        m = re.match(r"\s*([\w.\-/]+\.[\w.\-/]+)\s+v[\d.]", line)
        if m:
            deps.append(m.group(1))
    return deps


@manifest("Cargo.toml")
def _parse_cargo(p: Path) -> list[str]:
    try:
        data = tomllib.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return list(data.get("dependencies", {}))


@manifest("Gemfile")
def _parse_gemfile(p: Path) -> list[str]:
    return re.findall(r"^\s*gem\s+['\"]([\w\-]+)['\"]", p.read_text(errors="ignore"), re.M)


@manifest("composer.json")
def _parse_composer(p: Path) -> list[str]:
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return [k for k in data.get("require", {}) if k != "php"]


FRAMEWORK_HINTS = {
    "fastapi",
    "django",
    "flask",
    "sqlalchemy",
    "celery",
    "react",
    "vue",
    "next",
    "express",
    "nestjs",
    "spring",
    "pytorch",
    "tensorflow",
    "scikit-learn",
    "pandas",
    "numpy",
    "litellm",
    "langchain",
    "pydantic",
    "typer",
}

DATABASE_HINTS = {
    "psycopg",
    "psycopg2",
    "asyncpg",
    "pymongo",
    "redis",
    "mysqlclient",
    "prisma",
}

INFRA_HINTS = {
    "docker",
    "kubernetes",
    "gunicorn",
    "uvicorn",
    "pytest",
    "ruff",
    "vite",
    "webpack",
}


def collect_dependencies(repo: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for fname, parser in MANIFEST_PARSERS.items():
        f = repo / fname
        if not f.is_file():
            continue
        try:
            deps = parser(f)
        except Exception:
            deps = []
        if deps:
            found[fname] = sorted(set(deps))[:80]
    return found


def _classify_dep(name: str, bucket: dict[str, set[str]]) -> None:
    key = name.lower().split("/")[-1]
    if key in FRAMEWORK_HINTS or key.startswith("react") or key.startswith("@vitejs"):
        bucket["frameworks"].add(name)
        return
    if key in DATABASE_HINTS or any(
        x in key for x in ("postgres", "mongo", "mysql", "sqlite", "redis")
    ):
        bucket["databases"].add(name)
        return
    if key in INFRA_HINTS or any(x in key for x in ("docker", "k8s", "aws", "gcp", "azure")):
        bucket["tools_and_infra"].add(name)
        return
    bucket["other"].add(name)


def detect_tech_stack(stats: RepoStatsBundle) -> TechStack:
    """Aggregate languages + classified dependencies into TechStack."""
    languages: set[str] = set()
    for lang in stats.summary.overall_language_share:
        if lang not in NON_CODE_LANGS:
            languages.add(lang)

    bucket: dict[str, set[str]] = {
        "frameworks": set(),
        "databases": set(),
        "tools_and_infra": set(),
        "other": set(),
    }

    for repo in stats.repos:
        for lang in repo.languages:
            if lang not in NON_CODE_LANGS:
                languages.add(lang)
        for deps in repo.dependencies.values():
            for dep in deps:
                _classify_dep(dep, bucket)

    return TechStack(
        languages=sorted(languages),
        frameworks=sorted(bucket["frameworks"]),
        databases=sorted(bucket["databases"]),
        tools_and_infra=sorted(bucket["tools_and_infra"]),
        other=sorted(bucket["other"])[:40],
    )


def detect_for_project(project: ProjectSummary) -> TechStack:
    share = {
        name: (stat.share or 0.0)
        for name, stat in project.languages.items()
        if stat.share is not None
    }
    bundle = RepoStatsBundle(
        generated_at="",
        summary=StatsSummary(repo_count=1, overall_language_share=share),
        repos=[project],
    )
    return detect_tech_stack(bundle)
