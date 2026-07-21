"""End-to-end analyze pipeline: mine → tech → fact sheet → profile."""

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path

from repo2resume.analysis.cache import (
    get_cached_profile,
    get_cached_project,
    profile_cache_key,
    repo_cache_key,
    set_cached_profile,
    set_cached_project,
)
from repo2resume.analysis.fact_sheet import build_fact_sheet
from repo2resume.analysis.git_miner import MineOptions, build_summary, mine_one, mine_repos
from repo2resume.analysis.profiler import Profiler
from repo2resume.analysis.tech_detector import detect_tech_stack
from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import CacheBackend
from repo2resume.storage.db import Database
from repo2resume.storage.models import FactSheet, ProjectSummary, RepoStatsBundle, SkillProfile

logger = logging.getLogger(__name__)


def mine_with_cache(
    paths: list[Path],
    options: MineOptions,
    cache: CacheBackend | None,
    *,
    use_cache: bool = True,
) -> RepoStatsBundle:
    if cache is None or not use_cache:
        return mine_repos(paths, options)

    results: list[ProjectSummary] = []
    errors: list[dict[str, str]] = []
    for raw in paths:
        repo = Path(raw).expanduser().resolve()
        try:
            from repo2resume.analysis.git_miner import short_head

            head = short_head(repo)
            key = repo_cache_key(repo, head, options)
            cached = get_cached_project(cache, key)
            if cached is not None and cached.head_commit == head:
                logger.info("cache hit %s", key)
                results.append(cached)
                continue
            project = mine_one(repo, options)
            set_cached_project(cache, key, project)
            results.append(project)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine failed for %s: %s", repo, exc)
            errors.append({"path": str(repo), "error": str(exc)[:300]})

    from datetime import datetime

    return RepoStatsBundle(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        author_filters=list(options.authors),
        since=options.since,
        summary=build_summary(results),
        repos=results,
        errors=errors,
    )


def safe_fact_sheet(stats: RepoStatsBundle) -> FactSheet:
    """Call 【手写】builder; fall back to empty sheet until implemented."""
    try:
        return build_fact_sheet(stats)
    except NotImplementedError:
        logger.warning("fact_sheet.build_fact_sheet not implemented yet — using empty FactSheet")
        return FactSheet(entries=[])


def run_analyze(
    paths: list[Path],
    options: MineOptions,
    *,
    config: AppConfig,
    cache: CacheBackend | None,
    db: Database | None,
    stats_only: bool = False,
    use_cache: bool = True,
) -> tuple[RepoStatsBundle, SkillProfile | None]:
    stats = mine_with_cache(paths, options, cache, use_cache=use_cache)
    if stats_only:
        return stats, None

    tech = detect_tech_stack(stats)
    facts = safe_fact_sheet(stats)

    profile: SkillProfile | None = None
    if cache is not None and use_cache:
        pkey = profile_cache_key(stats)
        profile = get_cached_profile(cache, pkey)

    if profile is None:
        llm = LLMClient(config, cache=cache)
        profiler = Profiler(llm)
        profile = profiler.build_profile(stats, tech_hints=tech, fact_sheet=facts)
        profile.source_stats_hash = profile_cache_key(stats)
        if cache is not None and use_cache:
            set_cached_profile(cache, profile_cache_key(stats), profile)

    if db is not None and profile is not None:
        db.conn.execute(
            """
            INSERT INTO skill_profiles(label, payload_json)
            VALUES (?, ?)
            """,
            (profile.primary_direction, profile.model_dump_json()),
        )
        db.conn.commit()

    return stats, profile
