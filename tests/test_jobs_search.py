"""Tests for jobs/search.py（含 Bocha 源）。"""

from __future__ import annotations

from datetime import date
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


def test_search_jobs_persists_layered_candidates(data_dir):
    db = Database(data_dir / "layered.db")
    jobs = search_jobs("Python backend", count=2, db=db, source="mock")
    assert jobs
    run = db.load_latest_search_run()
    assert run is not None
    candidates = db.list_job_candidates(run.run_id)
    assert candidates
    assert candidates[0].candidate_id
    assert candidates[0].discovery.title


def test_search_jobs_tavily_without_key():
    jobs = search_jobs(
        "Python backend",
        count=3,
        source="tavily",
        config=AppConfig(tavily_api_key=None),
    )
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

    assert mock_client.post.call_args.args[0] == BOCHA_WEB_SEARCH_URL
    body0 = mock_client.post.call_args_list[0].kwargs["json"]
    assert body0["summary"] is True
    assert ".." in body0["freshness"]
    start, end = body0["freshness"].split("..", 1)
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 182
    assert "site:www.liepin.com/job" in body0["query"]
    assert "site:zhipin.com/job_detail" not in body0["query"]


def test_bocha_source_handles_http_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.side_effect = httpx.HTTPError("boom")
    src = BochaJobSource("test-key", client=mock_client)
    assert src.fetch("Python", 3) == []


def test_detail_queries_keep_existing_site_operator() -> None:
    from repo2resume.jobs.search import _detail_oriented_queries, _domains_for_query

    qs = _detail_oriented_queries(
        "Python 后端工程师 校招 site:jobs.bytedance.com", locale="zh-CN"
    )
    assert qs == ["Python 后端工程师 校招 site:jobs.bytedance.com"]
    assert _domains_for_query(qs[0])[0] == "jobs.bytedance.com"


def test_detail_queries_cover_major_zh_boards() -> None:
    from repo2resume.jobs.search import _detail_oriented_queries, _domains_for_query

    qs = _detail_oriented_queries("Python 后端", locale="zh-CN")
    assert len(qs) == 5
    assert "zhipin.com/job_detail" in qs[0]
    blob = " ".join(qs)
    assert "liepin.com" in blob
    assert "talent.alibaba.com" in blob
    assert _domains_for_query(qs[0]) == ["zhipin.com", "www.zhipin.com"]


def test_is_job_detail_url_accepts_detail_rejects_search():
    from repo2resume.jobs.search import _is_job_detail_url

    assert _is_job_detail_url("https://www.zhipin.com/job_detail/e4f08e0c703ae85e1Hd609i7GFpV.html")
    assert _is_job_detail_url("https://www.liepin.com/job/1979220259.shtml")
    assert _is_job_detail_url("https://www.liepin.com/a/1979220259.shtml")
    assert _is_job_detail_url("https://jobs.51job.com/hangzhou-yhq/172869446.html")
    assert _is_job_detail_url("https://www.zhaopin.com/jobdetail/CCL1520545670J40863206401.htm")
    assert _is_job_detail_url("https://jobs.zhaopin.com/CC229685980J40778543205.htm")
    # Boss 关键词搜索页 —— 必须拒
    assert not _is_job_detail_url("https://www.zhipin.com/zhaopin/ae7868255d0e25bd1Hx529S6")
    assert not _is_job_detail_url("https://msearch.51job.com/jobs/shenzhen/158.html")
    assert not _is_job_detail_url("https://join.qq.com/post_detail.html?postid=")
    assert _is_job_detail_url(
        "https://join.qq.com/post_detail.html?postid=1147651181158656035"
    )


def test_listing_aggregate_keeps_detail_titles_with_zhaopin_info():
    from repo2resume.jobs.search import _is_listing_aggregate

    detail = Job(
        id="d1",
        title="Python后端工程师招聘信息",
        url="https://www.zhipin.com/job_detail/abc.html",
        jd_text="FastAPI",
    )
    assert not _is_listing_aggregate(detail)
    # 「这个职位」不应被「个职位」误杀
    assert not _is_listing_aggregate(
        Job(id="d2", title="关于这个职位", url="https://www.liepin.com/job/1.shtml", jd_text="x")
    )
    assert _is_listing_aggregate(
        Job(id="l1", title="1585 jobs in Beijing", url="https://example.com/x", jd_text="x")
    )
    assert _is_listing_aggregate(
        Job(id="l2", title="附近15个职位", url="https://example.com/x", jd_text="x")
    )


def test_unusable_hits_drop_salary_seo_and_login_wall():
    from repo2resume.jobs.search import _is_unusable_hit

    assert _is_unusable_hit(
        Job(
            id="seo",
            title="腾讯2025年异构编译工程师工资待遇 - BOSS直聘",
            url="https://www.zhipin.com/job_detail/x.html",
            jd_text="登录注册后可以： * 找工作",
        )
    )
    assert _is_unusable_hit(
        Job(
            id="evt",
            title="【3/27北京IBM校聘活动】AI算法工程师",
            url="https://www.liepin.com/job/1.shtml",
            jd_text="校招活动",
        )
    )
    assert not _is_unusable_hit(
        Job(
            id="ok",
            title="Python 后端工程师",
            url="https://www.liepin.com/job/1.shtml",
            jd_text="负责 Django API",
        )
    )
    assert not _is_unusable_hit(
        Job(
            id="boss-snippet",
            title="Python 后端工程师",
            url="https://www.zhipin.com/job_detail/abc.html",
            jd_text="登录注册后可以：找工作。岗位职责：Python FastAPI 后端。",
        )
    )
    assert _is_unusable_hit(
        Job(
            id="wenku",
            title="上海Python招聘2025年12月名企招聘信息-前程无忧职场文库",
            url="https://mwenku.51job.com/shanghai_jobs/202512/Python",
            jd_text="Python",
        )
    )
    assert _is_unusable_hit(
        Job(
            id="campus-old",
            title="汇丰软件开发（广东）有限公司2024校园招聘-前程无忧51job",
            url="http://campus.51job.com/m/HTC2024/positions.html",
            jd_text="2024 校园招聘 职位列表",
        )
    )


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
    assert len(jobs) >= 1
    assert any("job_detail" in (j.url or "") for j in jobs)


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
    payload = mock_http.post.call_args.kwargs["json"]
    assert "time_range" not in payload
    start = date.fromisoformat(payload["start_date"])
    end = date.fromisoformat(payload["end_date"])
    assert (end - start).days == 182
    assert mock_http.post.call_args.args[0] == "https://api.tavily.com/search"


def test_tavily_http_retries_bad_parameter_without_dates():
    calls: list[dict] = []

    def _post(url, json=None, timeout=30.0):
        del url, timeout
        payload = json or {}
        calls.append(payload)
        resp = MagicMock()
        if "start_date" in payload:
            resp.status_code = 400
            resp.text = '{"detail":{"error":"bad parameter or other API misuse"}}'
            return resp
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "results": [
                {
                    "title": "Python 后端",
                    "url": "https://www.liepin.com/job/1979220259.shtml",
                    "content": "Django",
                }
            ]
        }
        return resp

    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.side_effect = _post
    src = TavilyJobSource("tvly-test", http_client=mock_http, prefer_zh=True)
    jobs = src.fetch("Python 后端 北京 校招", 2)
    assert jobs
    assert any("start_date" not in p for p in calls)
    assert any("start_date" in p for p in calls)


def test_tavily_one_path_always_400_does_not_empty_fetch():
    """5 路里 zhipin 一路始终 400 时，其它路结果仍应回来。"""

    def _post(url, json=None, timeout=30.0):
        del url, timeout
        payload = json or {}
        query = str(payload.get("query") or "")
        resp = MagicMock()
        if "zhipin.com" in query:
            resp.status_code = 400
            resp.text = '{"detail":{"error":"bad parameter or other API misuse"}}'
            return resp
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "results": [
                {
                    "title": "Python 后端",
                    "url": "https://www.liepin.com/job/1979220259.shtml",
                    "content": "Django",
                }
            ]
        }
        return resp

    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.side_effect = _post
    src = TavilyJobSource("tvly-test", http_client=mock_http, prefer_zh=True)
    jobs = src.fetch("Python 后端 上海 校招", 2)
    assert jobs
    assert jobs[0].url and "liepin.com" in jobs[0].url


def test_counts_toward_quota_drops_unknown_keeps_detail_and_mock():
    from repo2resume.jobs.search import _counts_toward_quota

    assert _counts_toward_quota(
        Job(
            id="d",
            title="Python",
            url="https://www.liepin.com/job/1.shtml",
            source="tavily",
        )
    )
    assert not _counts_toward_quota(
        Job(
            id="u",
            title="文库",
            url="https://mwenku.51job.com/shanghai_jobs/202512/Python",
            source="tavily",
        )
    )
    assert _counts_toward_quota(
        Job(id="m", title="Mock Backend", source="mock")
    )


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


def test_search_jobs_auto_does_not_use_bocha_when_tavily_sparse():
    cfg = AppConfig(bocha_api_key="k", tavily_api_key="t")
    tavily_jobs = [
        Job(
            id="tavily-1",
            title="猎聘岗",
            jd_text="Python Django",
            skills=["Python"],
            source="tavily",
            url="https://www.liepin.com/job/1.shtml",
        )
    ]
    bocha_jobs = [
        Job(
            id="bocha-1",
            title="Boss岗",
            jd_text="Python FastAPI",
            skills=["Python"],
            source="bocha",
            url="https://www.zhipin.com/job_detail/abc.html",
        )
    ]

    with (
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
        patch("repo2resume.jobs.search.BochaJobSource") as bocha_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = tavily_jobs
        tavily_cls.return_value = tavily
        bocha = MagicMock()
        bocha.fetch.return_value = bocha_jobs
        bocha_cls.return_value = bocha
        jobs = search_jobs("Python backend", count=5, config=cfg, source="auto")

    assert [j.id for j in jobs] == ["tavily-1"]
    tavily.fetch.assert_called_once()
    bocha_cls.assert_not_called()


def test_search_jobs_auto_returns_empty_instead_of_mock_when_key_configured():
    """配了联网 key 却召回为空时不能塞示例职位——假岗没有链接，无法投递。"""
    cfg = AppConfig(bocha_api_key="k", tavily_api_key="t")

    with (
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
        patch("repo2resume.jobs.search.BochaJobSource") as bocha_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = []
        tavily_cls.return_value = tavily
        jobs = search_jobs("Python backend", count=2, config=cfg, source="auto")

    assert jobs == []
    tavily.fetch.assert_called_once()
    bocha_cls.assert_not_called()


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


def test_search_jobs_auto_uses_alibaba_top_when_configured_and_tavily_empty():
    cfg = AppConfig(
        tavily_api_key="t",
        alibaba_top_app_key="ak",
        alibaba_top_app_secret="as",
    )
    ali_jobs = [
        Job(
            id="alibaba-1",
            title="阿里后端",
            jd_text="Python 后端开发",
            source="alibaba_top",
            url="https://talent.alibaba.com/position/detail?positionCode=GP1",
        )
    ]
    with (
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
        patch("repo2resume.jobs.search.AlibabaTopSource") as ali_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = []
        tavily_cls.return_value = tavily
        ali = MagicMock()
        ali.fetch.return_value = ali_jobs
        ali_cls.return_value = ali
        jobs = search_jobs("Python backend", count=1, config=cfg, source="auto")

    assert jobs
    assert jobs[0].source == "alibaba_top"
    tavily.fetch.assert_called_once()
    ali.fetch.assert_called_once()
