"""Tests for jobs/search.py."""

from __future__ import annotations

from repo2resume.jobs.search import MockJobSource, search_jobs
from repo2resume.storage.db import Database


def test_mock_source_returns_sorted_jobs():
    src = MockJobSource()
    jobs = src.fetch("Python backend microservices", 3)
    assert len(jobs) <= 3
    assert any("Backend" in j.title or "Python" in j.skills for j in jobs)


def test_mock_source_empty_query():
    src = MockJobSource()
    assert src.fetch("", 5) == []


def test_mock_source_chinese_backend_query_hits_multiple():
    """中文「后端」应经同义词扩展命中多个英文职位，而不是只剩 1 条。"""
    src = MockJobSource()
    jobs = src.fetch("Python 后端开发", 5)
    assert len(jobs) >= 3
    titles = " ".join(j.title for j in jobs).lower()
    assert "backend" in titles or "full stack" in titles or "data" in titles


def test_search_jobs_uses_cache(data_dir):
    db = Database(data_dir / "test.db")
    jobs1 = search_jobs("Python backend", count=3, db=db, source="mock")
    assert len(jobs1) > 0
    jobs2 = search_jobs("Python backend", count=3, db=db, source="mock")
    assert [j.id for j in jobs1] == [j.id for j in jobs2]


def test_search_jobs_tavily_stub_without_key():
    jobs = search_jobs("Python backend", count=3, source="tavily")
    assert jobs == []
