"""Tests for jobs/search.py（含 Bocha 源）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from repo2resume.config import AppConfig
from repo2resume.jobs.search import (
    BOCHA_WEB_SEARCH_URL,
    BochaJobSource,
    MockJobSource,
    TavilyJobSource,
    search_jobs,
)
from repo2resume.storage.db import Database
from repo2resume.storage.models import Job


def test_mock_source_returns_sorted_jobs():
    src = MockJobSource()
    jobs = src.fetch("Python backend microservices", 3)
    assert len(jobs) <= 3
    assert any("Backend" in j.title or "Python" in j.skills for j in jobs)


def test_mock_source_empty_query():
    src = MockJobSource()
    assert src.fetch("", 5) == []


def test_mock_source_chinese_backend_query_hits_multiple():
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


def test_search_jobs_persists_individual_job_rows(data_dir):
    """搜岗后应按 job.id 落库，generate_resume(job_id=...) 才能查到。"""
    from repo2resume.jobs.search import persist_jobs

    db = Database(data_dir / "persist.db")
    jobs = search_jobs("Python backend", count=3, db=db, source="mock")
    assert jobs
    row = db.conn.execute(
        "SELECT id, title, jd_text FROM jobs WHERE id = ?",
        (jobs[0].id,),
    ).fetchone()
    assert row is not None
    assert row["title"] == jobs[0].title
    assert (row["jd_text"] or "") == (jobs[0].jd_text or "")

    # 缓存命中路径也应回填单行
    jobs2 = search_jobs("Python backend", count=3, db=db, source="mock")
    assert jobs2[0].id == jobs[0].id
    persist_jobs(db, [])  # no-op



def test_search_jobs_tavily_without_key():
    jobs = search_jobs("Python backend", count=3, source="tavily", config=AppConfig(tavily_api_key=None))
    assert jobs == []


def test_search_jobs_auto_without_key_falls_back_to_mock():
    cfg = AppConfig(bocha_api_key=None, tavily_api_key=None)
    jobs = search_jobs("Python backend", count=3, config=cfg, source="auto")
    assert len(jobs) > 0
    assert all(j.source == "mock" for j in jobs)


def test_search_jobs_bocha_without_key_returns_empty():
    cfg = AppConfig(bocha_api_key=None)
    assert search_jobs("Python", count=3, config=cfg, source="bocha") == []


def test_bocha_source_parses_web_pages():
    sample = {
        "webPages": {
            "value": [
                {
                    "name": "Python 后端工程师",
                    "url": "https://www.zhipin.com/job_detail/abc.html",
                    "siteName": "BOSS直聘",
                    "snippet": "负责 API 开发",
                    "summary": "使用 FastAPI 与 Redis 构建服务。",
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = sample

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = mock_resp

    src = BochaJobSource("test-key", client=mock_client)
    jobs = src.fetch("Python 后端", 5)
    assert len(jobs) == 1
    assert jobs[0].source == "bocha"
    assert jobs[0].url == "https://www.zhipin.com/job_detail/abc.html"

    call_kwargs = mock_client.post.call_args
    assert call_kwargs.args[0] == BOCHA_WEB_SEARCH_URL
    body = call_kwargs.kwargs["json"]
    assert body["summary"] is True


def test_bocha_source_handles_http_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.side_effect = httpx.HTTPError("boom")
    src = BochaJobSource("test-key", client=mock_client)
    assert src.fetch("Python", 3) == []


def test_is_job_detail_url_accepts_detail_rejects_search():
    from repo2resume.jobs.search import _is_job_detail_url

    assert _is_job_detail_url(
        "https://www.zhipin.com/job_detail/e4f08e0c703ae85e1Hd609i7GFpV.html"
    )
    assert _is_job_detail_url("https://www.liepin.com/job/1979220259.shtml")
    assert _is_job_detail_url("https://jobs.51job.com/hangzhou-yhq/172869446.html")
    assert _is_job_detail_url("https://www.zhaopin.com/jobdetail/CCL1520545670J40863206401.htm")
    # Boss 关键词搜索页 —— 必须拒
    assert not _is_job_detail_url("https://www.zhipin.com/zhaopin/ae7868255d0e25bd1Hx529S6")
    assert not _is_job_detail_url("https://msearch.51job.com/jobs/shenzhen/158.html")


def test_tavily_filters_non_detail_urls():
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "title": "python后端招聘信息",
                "url": "https://www.zhipin.com/zhaopin/ae7868255d0e25bd1Hx529S6",
                "content": "列表页",
            },
            {
                "title": "Python 后端工程师",
                "url": "https://www.zhipin.com/job_detail/abc123.html",
                "content": "岗位职责：后端开发。任职要求：FastAPI。",
            },
        ]
    }
    src = TavilyJobSource("tvly-test", client=mock_client, prefer_zh=True)
    jobs = src.fetch("Python 后端", 5)
    assert len(jobs) == 1
    assert "job_detail" in (jobs[0].url or "")


def test_tavily_source_parses_results():
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "title": "Backend Engineer",
                "url": "https://www.zhipin.com/job_detail/good.html",
                "content": "Python FastAPI Redis",
            }
        ]
    }
    src = TavilyJobSource("tvly-test", client=mock_client)
    jobs = src.fetch("Python backend", 3)
    assert len(jobs) >= 1
    assert jobs[0].source == "tavily"
    assert jobs[0].id.startswith("tavily-")
    assert "FastAPI" in jobs[0].jd_text
    assert mock_client.search.call_count >= 1


def test_tavily_http_fallback_parses_results():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "Python 后端",
                "url": "https://www.liepin.com/job/1979220259.shtml",
                "content": "Django PostgreSQL",
            }
        ]
    }
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.return_value = mock_resp
    src = TavilyJobSource("tvly-test", http_client=mock_http)
    jobs = src.fetch("Python", 2)
    assert len(jobs) >= 1
    assert jobs[0].source == "tavily"
    assert mock_http.post.call_args.args[0] == "https://api.tavily.com/search"


def test_bocha_401_marks_auth_failed(monkeypatch):
    from repo2resume.jobs import search as search_mod

    monkeypatch.setattr(search_mod, "_AUTH_FAILED_SOURCES", set())
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = mock_resp
    src = BochaJobSource("bad-key", client=mock_client)
    assert src.fetch("Python", 3) == []
    assert "bocha" in search_mod._AUTH_FAILED_SOURCES
    # 第二次应直接跳过，不再 POST
    mock_client.post.reset_mock()
    assert src.fetch("Python", 3) == []
    mock_client.post.assert_not_called()


def test_search_jobs_auto_uses_tavily_first_when_both_keys():
    cfg = AppConfig(bocha_api_key="k", tavily_api_key="t")
    fake_jobs = MockJobSource().fetch("Python backend", 2)
    for j in fake_jobs:
        j.source = "tavily"

    with (
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
        patch("repo2resume.jobs.search.BochaJobSource") as bocha_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = fake_jobs
        tavily_cls.return_value = tavily
        jobs = search_jobs("Python backend", count=2, config=cfg, source="auto")
    assert len(jobs) == 2
    assert all(j.source == "tavily" for j in jobs)
    tavily_cls.assert_called_once()
    bocha_cls.assert_not_called()


def test_search_jobs_auto_falls_back_to_bocha_when_tavily_empty():
    cfg = AppConfig(bocha_api_key="k", tavily_api_key="t")
    bocha_jobs = [
        Job(
            id="bocha-1",
            title="Python Backend",
            company="Acme",
            jd_text="Python Django",
            skills=["Python"],
            source="bocha",
            url="https://example.com/j/1",
        )
    ]

    with (
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
        patch("repo2resume.jobs.search.BochaJobSource") as bocha_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = []
        tavily_cls.return_value = tavily
        bocha = MagicMock()
        bocha.fetch.return_value = bocha_jobs
        bocha_cls.return_value = bocha
        jobs = search_jobs("Python backend", count=2, config=cfg, source="auto")

    assert len(jobs) == 1
    assert jobs[0].source == "bocha"
    tavily.fetch.assert_called_once()
    bocha.fetch.assert_called_once()


def test_search_jobs_auto_prefers_tavily_when_only_tavily_key():
    cfg = AppConfig(bocha_api_key=None, tavily_api_key="t")
    tavily_jobs = MockJobSource().fetch("Python backend", 1)
    for j in tavily_jobs:
        j.source = "tavily"

    with patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls:
        inst = MagicMock()
        inst.fetch.return_value = tavily_jobs
        tavily_cls.return_value = inst
        jobs = search_jobs("Python backend", count=1, config=cfg, source="auto")

    assert jobs[0].source == "tavily"
    tavily_cls.assert_called_once()
