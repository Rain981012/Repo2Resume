"""分析结果缓存键与读写：把 stats/profile 序列化进 `CacheBackend`。

缓存键由「仓库路径 hash + head commit + 作者过滤 + since」组成，head 一变键就变，
天然失效，所以无需 TTL。profile 的键还把 summary 纳入 hash，保证统计变了画像也重算。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo2resume.analysis.git_miner import MineOptions
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.models import ProjectSummary, RepoStatsBundle, SkillProfile

PROFILE_CACHE_SCHEMA = "v2"


def _hash_text(text: str) -> str:
    """对文本取 sha256 前 16 位，用于把长字符串（路径/作者列表）压成短摘要进缓存键。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def repo_cache_key(repo: Path, head: str, options: MineOptions) -> str:
    """单仓库统计的缓存键：`analysis:{path_hash}:{head}:{authors_hash}:{since_hash}`。"""
    authors = ",".join(sorted(options.authors))
    since = options.since or ""
    path_hash = _hash_text(str(repo.resolve()))
    return f"analysis:{path_hash}:{head}:{_hash_text(authors)}:{_hash_text(since)}"


def bundle_cache_key(paths: list[Path], options: MineOptions, heads: list[str]) -> str:
    """多仓库 bundle 的缓存键：把所有 `path:head` 排序后整体 hash，避免顺序影响命中。"""
    parts = sorted(f"{p.resolve()}:{h}" for p, h in zip(paths, heads, strict=False))
    authors = ",".join(sorted(options.authors))
    since = options.since or ""
    digest = _hash_text("|".join(parts) + f"|{authors}|{since}")
    return f"analysis:bundle:{digest}"


def get_cached_project(cache: CacheBackend, key: str) -> ProjectSummary | None:
    """命中则反序列化成 `ProjectSummary`，未命中返回 None。"""
    raw = cache.get(key)
    if raw is None:
        return None
    return ProjectSummary.model_validate_json(raw)


def set_cached_project(cache: CacheBackend, key: str, project: ProjectSummary) -> None:
    # 无 TTL：key 里已含 head commit，head 变即失效
    cache.set(key, project.model_dump_json(), ttl=None)


def get_cached_profile(cache: CacheBackend, key: str) -> SkillProfile | None:
    """命中则反序列化成 `SkillProfile`，未命中返回 None。"""
    raw = cache.get(key)
    if raw is None:
        return None
    return SkillProfile.model_validate_json(raw)


def set_cached_profile(cache: CacheBackend, key: str, profile: SkillProfile) -> None:
    cache.set(key, profile.model_dump_json(), ttl=None)


def profile_cache_key(stats: RepoStatsBundle) -> str:
    """画像缓存键：把作者/since/各仓 head/summary 整体 hash，统计变则画像重算。"""
    payload = json.dumps(
        {
            "schema": PROFILE_CACHE_SCHEMA,
            "authors": stats.author_filters,
            "since": stats.since,
            "heads": [(r.path, r.head_commit) for r in stats.repos],
            "summary": stats.summary.model_dump(),
        },
        sort_keys=True,
    )
    return f"profile:{_hash_text(payload)}"
