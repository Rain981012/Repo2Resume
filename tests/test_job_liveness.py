"""职位详情页在招核对。"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from repo2resume.jobs.liveness import keep_open_jobs, listing_status_from_html, probe_listing
from repo2resume.storage.models import Job


def test_listing_status_detects_liepin_offline() -> None:
    html = "<html><body>很抱歉，职位已下线</body></html>"
    assert listing_status_from_html(html) == "offline"


def test_listing_status_detects_expired_marker() -> None:
    html = "<html><body>该职位已停止招聘，请查看其他岗位</body></html>"
    assert listing_status_from_html(html) == "offline"


def test_listing_status_live_without_negative() -> None:
    html = "<html><body>职位详情 立即投递 Python 后端</body></html>"
    assert listing_status_from_html(html) == "live"


def test_probe_404_is_offline() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 404
    resp.text = ""
    client.get.return_value = resp
    assert probe_listing("https://www.liepin.com/job/1", client=client) == "offline"


def test_probe_403_is_unknown_not_dropped() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 403
    resp.text = ""
    resp.url = "https://www.zhipin.com/job_detail/x"
    client.get.return_value = resp
    assert probe_listing("https://www.zhipin.com/job_detail/x", client=client) == "unknown"


def test_probe_boss_security_interstitial_is_blocked() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.zhipin.com/web/passport/zp/security.html?seed=1"
    resp.text = "<title>请稍候 - BOSS直聘</title>"
    client.get.return_value = resp
    assert probe_listing("https://www.zhipin.com/job_detail/x", client=client) == "blocked"


def test_probe_detail_redirect_to_listing_marks_offline() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.zhaopin.com/jobs/searchresult.ashx?jl=530"
    resp.text = "<html><body>搜索结果</body></html>"
    client.get.return_value = resp
    assert (
        probe_listing("https://jobs.zhaopin.com/CC229685980J40778543205.htm", client=client)
        == "offline"
    )


def test_probe_stale_campus_campaign_marks_offline() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "http://campus.51job.com/m/HTC2024/positions.html"
    resp.text = "<html><body>2024校园招聘 职位列表</body></html>"
    client.get.return_value = resp
    assert (
        probe_listing("http://campus.51job.com/m/HTC2024/positions.html", client=client)
        == "offline"
    )


def test_keep_open_jobs_skips_http_for_boss() -> None:
    client = MagicMock(spec=httpx.Client)
    job = Job(
        id="boss-shell",
        title="Python 后端",
        source="tavily",
        url="https://www.zhipin.com/job_detail/x",
        jd_text="岗位职责：Python FastAPI。登录注册后可以找工作。",
    )
    kept, n_drop = keep_open_jobs([job], client=client)
    assert n_drop == 0
    assert [j.id for j in kept] == ["boss-shell"]
    client.get.assert_not_called()


def test_keep_open_jobs_drops_offline_keeps_mock() -> None:
    live = Job(
        id="a",
        title="Backend",
        source="tavily",
        url="https://www.liepin.com/job/1",
    )
    dead = Job(
        id="b",
        title="CAD",
        source="tavily",
        url="https://www.liepin.com/job/2",
    )
    mock = Job(id="m", title="Mock", source="mock")

    client = MagicMock(spec=httpx.Client)

    def _get(url, **kwargs):
        resp = MagicMock()
        resp.url = url
        if url.endswith("/2"):
            resp.status_code = 200
            resp.text = "职位已下线"
        else:
            resp.status_code = 200
            resp.text = "立即投递"
        return resp

    client.get.side_effect = _get
    kept, n_drop = keep_open_jobs([live, dead, mock], client=client)
    assert n_drop == 1
    assert [j.id for j in kept] == ["a", "m"]


def test_probe_zhaopin_security_interstitial_is_blocked() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.zhaopin.com/jobdetail/CC229685980J40778543205.htm"
    resp.text = (
        '<!doctype html><html lang="en"><title>Security Verification</title>'
        "<body>Please wait</body></html>"
    )
    client.get.return_value = resp
    assert (
        probe_listing(
            "https://jobs.zhaopin.com/CC229685980J40778543205.htm",
            client=client,
        )
        == "blocked"
    )


def test_keep_open_jobs_drops_zhaopin_security_wall() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.zhaopin.com/jobdetail/x.htm"
    resp.text = "<title>Security Verification</title>"
    client.get.return_value = resp
    job = Job(
        id="zp",
        title="Python 后端",
        source="tavily",
        url="https://www.zhaopin.com/jobdetail/x.htm",
    )
    kept, n_drop = keep_open_jobs([job], client=client)
    assert n_drop == 1
    assert kept == []


def test_keep_open_jobs_drops_blocked_non_boss() -> None:
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.liepin.com/web/passport/security.html"
    resp.text = "请稍候"
    client.get.return_value = resp
    job = Job(
        id="lp",
        title="后端",
        source="tavily",
        url="https://www.liepin.com/job/1",
    )
    kept, n_drop = keep_open_jobs([job], client=client)
    assert n_drop == 1
    assert kept == []
