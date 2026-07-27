"""职位搜索：mock 源 + Tavily 接口 stub + SQLite 缓存。

【AI 辅助】模块：先确认接口（JobSource），再填实现。
Phase 3 先让 mock 源跑通检索与 matcher；Tavily 在接口 stub 中预留，配置 key 后启用。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from repo2resume.config import AppConfig
from repo2resume.storage.db import Database
from repo2resume.storage.models import Job

logger = logging.getLogger(__name__)

JOB_CACHE_TTL_HOURS = 24

_DEFAULT_MOCK_JOBS: list[dict[str, Any]] = [
    {
        "id": "mock-backend-1",
        "title": "Senior Backend Engineer",
        "company": "CloudScale",
        "location": "Remote",
        "jd_text": "Design and build high-performance distributed systems. "
        "Experience with Python, Go, microservices, Kubernetes, and PostgreSQL. "
        "Lead API design and CI/CD pipelines.",
        "skills": ["Python", "Go", "Kubernetes", "PostgreSQL", "microservices", "CI/CD"],
        "source": "mock",
    },
    {
        "id": "mock-frontend-1",
        "title": "Frontend Engineer",
        "company": "PixelPerfect",
        "location": "San Francisco",
        "jd_text": "Build responsive web apps with React, TypeScript, and Next.js. "
        "Collaborate with designers and backend engineers.",
        "skills": ["React", "TypeScript", "Next.js", "CSS"],
        "source": "mock",
    },
    {
        "id": "mock-data-1",
        "title": "Data Engineer",
        "company": "DataFlow",
        "location": "Remote",
        "jd_text": "Build data pipelines and warehouses. Strong Python, SQL, Spark, and Kafka. "
        "Experience with data modeling and ETL orchestration.",
        "skills": ["Python", "SQL", "Spark", "Kafka", "ETL"],
        "source": "mock",
    },
    {
        "id": "mock-ml-1",
        "title": "Machine Learning Engineer",
        "company": "ModelMind",
        "location": "Remote",
        "jd_text": "Train and deploy ML models at scale. Python, PyTorch, transformers, MLOps. "
        "Experience with vector databases and retrieval systems.",
        "skills": ["Python", "PyTorch", "transformers", "MLOps", "vector databases"],
        "source": "mock",
    },
    {
        "id": "mock-fullstack-1",
        "title": "Full Stack Engineer",
        "company": "StartupXYZ",
        "location": "New York",
        "jd_text": "Own end-to-end features. Python/FastAPI backend, React frontend. "
        "DevOps experience with Docker and AWS is a plus.",
        "skills": ["Python", "FastAPI", "React", "Docker", "AWS"],
        "source": "mock",
    },
]


@runtime_checkable
class JobSource(Protocol):
    """职位源接口：给定 query，返回 Job 列表。"""

    def fetch(self, query: str, count: int) -> list[Job]:
        ...


# 中文方向词 → 英文检索词，避免「Python 后端开发」几乎只靠 "python" 命中。
_QUERY_SYNONYMS: dict[str, list[str]] = {
    "后端": ["backend", "django", "fastapi", "api"],
    "前端": ["frontend", "react", "typescript", "vue"],
    "全栈": ["full stack", "fullstack", "fastapi", "react"],
    "机器学习": ["machine learning", "ml", "pytorch", "transformers"],
    "数据科学": ["data", "python", "sql", "etl", "pandas"],
    "数据": ["data", "sql", "etl", "spark"],
}


def _expand_query_tokens(query: str) -> list[str]:
    """把 query 拆成 token，并展开中文同义词。"""
    query_lower = query.lower()
    tokens = list(query_lower.split())
    expanded = list(tokens)
    for zh, en_list in _QUERY_SYNONYMS.items():
        if zh in query_lower:
            expanded.extend(en_list)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for t in expanded:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


class MockJobSource:
    """内置示例职位，不依赖网络。"""

    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs = jobs or [Job.model_validate(j) for j in _DEFAULT_MOCK_JOBS]

    def fetch(self, query: str, count: int) -> list[Job]:
        if not query.strip():
            return []
        tokens = _expand_query_tokens(query)

        # 简单相关度：query（含中英同义词）命中 title / skills / jd_text 越多越靠前
        def score(job: Job) -> int:
            text = f"{job.title} {job.company or ''} {' '.join(job.skills)} {job.jd_text}".lower()
            return sum(1 for t in tokens if t in text)

        scored = sorted(self.jobs, key=score, reverse=True)
        # 至少保留有命中的职位；全 0 时仍按原序截断，避免空结果
        positive = [j for j in scored if score(j) > 0]
        return (positive or scored)[:count]


class TavilyJobSource:
    """Tavily 搜索职位 stub：有 key 时尝试调用，否则返回空列表。"""

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    def fetch(self, query: str, count: int) -> list[Job]:
        if not self.api_key:
            logger.warning("Tavily API key not configured; skipping Tavily search")
            return []
        # 实际接入：调用 tavily-python，解析结果，把网页摘要转成 Job
        raise NotImplementedError("Tavily search integration is a stub for Phase 3")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def _cache_key(query: str, count: int) -> str:
    payload = f"{query}:{count}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_cached_jobs(db: Database | None, key: str) -> list[Job] | None:
    if db is None:
        return None
    row = db.conn.execute(
        "SELECT payload_json FROM jobs WHERE id = ? AND updated_at > ?",
        (key, (datetime.now(UTC) - timedelta(hours=JOB_CACHE_TTL_HOURS)).isoformat()),
    ).fetchone()
    if row is None or not row["payload_json"]:
        return None
    try:
        data = json.loads(row["payload_json"])
        if isinstance(data, list):
            return [Job.model_validate(j) for j in data]
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _save_cached_jobs(db: Database | None, key: str, jobs: list[Job]) -> None:
    if db is None:
        return
    payload = json.dumps([j.model_dump() for j in jobs], ensure_ascii=False)
    db.conn.execute(
        "INSERT INTO jobs(id, title, company, location, jd_text, skills, source, updated_at, "
        "payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, company=excluded.company, "
        "location=excluded.location, jd_text=excluded.jd_text, skills=excluded.skills, "
        "source=excluded.source, updated_at=excluded.updated_at, "
        "payload_json=excluded.payload_json",
        (key, "cached", "", "", "", "", "cache", datetime.now(UTC).isoformat(), payload),
    )
    db.conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_jobs(
    query: str,
    *,
    count: int = 5,
    config: AppConfig | None = None,
    db: Database | None = None,
    source: str = "mock",
) -> list[Job]:
    """搜索职位：优先读缓存，否则调用 source。"""
    if not query.strip():
        return []
    if count <= 0:
        return []

    key = _cache_key(query, count)
    cached = _load_cached_jobs(db, key)
    if cached is not None:
        return cached

    if source == "tavily":
        src: JobSource = TavilyJobSource(getattr(config, "tavily_api_key", None))
    else:
        src = MockJobSource()

    jobs = src.fetch(query, count)
    _save_cached_jobs(db, key, jobs)
    return jobs
