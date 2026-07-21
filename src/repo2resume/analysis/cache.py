"""Analysis result cache: analysis:{repo_hash}:{head}:{authors}:{since}."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo2resume.analysis.git_miner import MineOptions
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.models import ProjectSummary, RepoStatsBundle, SkillProfile


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def repo_cache_key(repo: Path, head: str, options: MineOptions) -> str:
    authors = ",".join(sorted(options.authors))
    since = options.since or ""
    path_hash = _hash_text(str(repo.resolve()))
    return f"analysis:{path_hash}:{head}:{_hash_text(authors)}:{_hash_text(since)}"


def bundle_cache_key(paths: list[Path], options: MineOptions, heads: list[str]) -> str:
    parts = sorted(f"{p.resolve()}:{h}" for p, h in zip(paths, heads, strict=False))
    authors = ",".join(sorted(options.authors))
    since = options.since or ""
    digest = _hash_text("|".join(parts) + f"|{authors}|{since}")
    return f"analysis:bundle:{digest}"


def get_cached_project(cache: CacheBackend, key: str) -> ProjectSummary | None:
    raw = cache.get(key)
    if raw is None:
        return None
    return ProjectSummary.model_validate_json(raw)


def set_cached_project(cache: CacheBackend, key: str, project: ProjectSummary) -> None:
    # No TTL: invalidates when head commit in key changes
    cache.set(key, project.model_dump_json(), ttl=None)


def get_cached_profile(cache: CacheBackend, key: str) -> SkillProfile | None:
    raw = cache.get(key)
    if raw is None:
        return None
    return SkillProfile.model_validate_json(raw)


def set_cached_profile(cache: CacheBackend, key: str, profile: SkillProfile) -> None:
    cache.set(key, profile.model_dump_json(), ttl=None)


def profile_cache_key(stats: RepoStatsBundle) -> str:
    payload = json.dumps(
        {
            "authors": stats.author_filters,
            "since": stats.since,
            "heads": [(r.path, r.head_commit) for r in stats.repos],
            "summary": stats.summary.model_dump(),
        },
        sort_keys=True,
    )
    return f"profile:{_hash_text(payload)}"
