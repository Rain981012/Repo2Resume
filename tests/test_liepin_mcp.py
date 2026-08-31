"""猎聘官方 MCP 职位源。"""

from __future__ import annotations

import json

import httpx

from repo2resume.config import AppConfig, load_config, save_config
from repo2resume.jobs.liepin_mcp import (
    DEFAULT_LIEPIN_MCP_URL,
    LiepinMcpSource,
    jobs_from_mcp_payload,
    parse_mcp_http_body,
    split_liepin_query,
    unwrap_mcp_tool_result,
)
from repo2resume.jobs.search import _AUTH_FAILED_SOURCES, search_jobs
from repo2resume.storage.models import Job


def test_split_liepin_query_extracts_city():
    args = split_liepin_query("Python 后端工程师 上海 校招")
    assert args["jobName"] == "Python后端"
    assert args["address"] == "上海"


def test_split_liepin_query_without_city():
    args = split_liepin_query("FastAPI backend")
    assert args == {"jobName": "后端开发"}


def test_jobs_from_mcp_jsonrpc_text_content():
    inner = {
        "jobs": [
            {
                "jobId": "19123456789",
                "jobKind": "2",
                "jobName": "Python 后端",
                "companyName": "猎聘科技",
                "address": "上海",
                "salary": "25-40k",
                "jobDesc": "FastAPI",
                "jobDetailUrl": "https://www.liepin.com/job/19123456789.shtml",
                "location": "上海-徐汇",
            }
        ]
    }
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": json.dumps(inner, ensure_ascii=False)}]},
    }
    jobs = jobs_from_mcp_payload(payload, count=5)
    assert len(jobs) == 1
    assert jobs[0].title == "Python 后端"
    assert jobs[0].company == "猎聘科技"
    assert jobs[0].location == "上海-徐汇"
    assert jobs[0].source == "liepin_mcp"
    assert jobs[0].url.startswith("https://www.liepin.com/job/19123456789")
    assert "25-40k" in (jobs[0].jd_text or "")
    assert jobs[0].id.startswith("liepin-")


def test_jobs_from_nested_job_card():
    payload = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "data": [
                                {
                                    "job": {"jobId": "88", "jobName": "算法工程师"},
                                    "comp": {"compName": "某厂"},
                                }
                            ]
                        }
                    ),
                }
            ]
        }
    }
    jobs = jobs_from_mcp_payload(payload, count=3)
    assert jobs[0].title == "算法工程师"
    assert jobs[0].company == "某厂"
    assert jobs[0].url == "https://www.liepin.com/job/88"


def test_parse_mcp_sse_body():
    inner = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
    text = "event: message\ndata: " + json.dumps(inner) + "\n\n"
    parsed = parse_mcp_http_body(text, content_type="text/event-stream")
    assert parsed["result"]["ok"] is True


def test_unwrap_mcp_error_raises():
    try:
        unwrap_mcp_tool_result({"error": {"code": -32000, "message": "token expired"}})
    except RuntimeError as exc:
        assert "token expired" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def _mcp_transport(token: str = "good-token") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-user-token") != token:
            return httpx.Response(401, text="unauthorized")
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                headers={"mcp-session-id": "sid-1"},
            )
        if method == "tools/call":
            assert request.headers.get("mcp-session-id") == "sid-1"
            args = body["params"]["arguments"]
            jobs = [
                {
                    "jobId": "19111111111",
                    "jobName": args.get("jobName") or "职位",
                    "companyName": "猎聘",
                    "address": args.get("address") or "北京",
                    "salary": "30-45k",
                    "jobDesc": "Python",
                }
            ]
            text = json.dumps({"jobs": jobs}, ensure_ascii=False)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": text}]},
                },
            )
        return httpx.Response(400, text="unexpected method")

    return httpx.MockTransport(handler)


def test_liepin_mcp_source_fetch_maps_jobs():
    _AUTH_FAILED_SOURCES.discard("liepin_mcp")
    client = httpx.Client(transport=_mcp_transport(), timeout=5.0)
    src = LiepinMcpSource("good-token", mcp_url=DEFAULT_LIEPIN_MCP_URL, http_client=client)
    jobs = src.fetch("Python 后端 上海", 3)
    assert len(jobs) == 1
    assert jobs[0].title == "Python后端"
    assert jobs[0].location == "上海"
    assert jobs[0].url == "https://www.liepin.com/job/19111111111"


def test_liepin_mcp_source_401_marks_auth_failed():
    _AUTH_FAILED_SOURCES.discard("liepin_mcp")
    client = httpx.Client(transport=_mcp_transport(token="good-token"), timeout=5.0)
    src = LiepinMcpSource("bad-token", http_client=client)
    assert src.fetch("Python", 3) == []
    assert "liepin_mcp" in _AUTH_FAILED_SOURCES
    _AUTH_FAILED_SOURCES.discard("liepin_mcp")


def test_search_jobs_auto_uses_tavily_before_liepin():
    cfg = AppConfig(tavily_api_key="t", liepin_mcp_token="tok")
    tavily_jobs = [
        Job(
            id="tavily-1",
            title="Python 后端",
            jd_text="Python backend",
            source="tavily",
            url="https://www.liepin.com/job/19999999999",
        )
    ]

    from unittest.mock import MagicMock, patch

    with (
        patch("repo2resume.jobs.search.LiepinMcpSource") as liepin_cls,
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = tavily_jobs
        tavily_cls.return_value = tavily
        liepin = MagicMock()
        liepin.fetch.return_value = []
        liepin_cls.return_value = liepin
        jobs = search_jobs("Python backend", count=1, config=cfg, source="auto")

    assert jobs
    assert jobs[0].source == "tavily"
    tavily.fetch.assert_called_once()
    liepin.fetch.assert_not_called()


def test_search_jobs_auto_uses_liepin_when_tavily_empty():
    from unittest.mock import MagicMock, patch

    cfg = AppConfig(tavily_api_key="t", liepin_mcp_token="tok")
    liepin_jobs = [
        Job(
            id="liepin-1",
            title="猎聘后端",
            jd_text="Python 后端 薪资：30-45k",
            source="liepin_mcp",
            url="https://www.liepin.com/job/19123456789",
        )
    ]
    with (
        patch("repo2resume.jobs.search.LiepinMcpSource") as liepin_cls,
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = []
        tavily_cls.return_value = tavily
        liepin = MagicMock()
        liepin.fetch.return_value = liepin_jobs
        liepin_cls.return_value = liepin
        jobs = search_jobs("Python backend", count=1, config=cfg, source="auto")

    assert jobs[0].source == "liepin_mcp"
    tavily.fetch.assert_called_once()
    liepin.fetch.assert_called_once()


def test_search_jobs_auto_drops_hardware_then_fills_from_liepin():
    from unittest.mock import MagicMock, patch

    cfg = AppConfig(tavily_api_key="t", liepin_mcp_token="tok")
    with (
        patch("repo2resume.jobs.search.LiepinMcpSource") as liepin_cls,
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = [
            Job(
                id="tavily-fpga",
                title="FPGA工程师",
                jd_text="芯片设计",
                source="tavily",
                url="https://www.liepin.com/job/19111111111",
            )
        ]
        tavily_cls.return_value = tavily
        liepin = MagicMock()
        liepin.fetch.return_value = [
            Job(
                id="liepin-1",
                title="Python 后端开发",
                jd_text="FastAPI",
                source="liepin_mcp",
                url="https://www.liepin.com/job/19123456789",
            )
        ]
        liepin_cls.return_value = liepin
        jobs = search_jobs("Python 后端工程师 上海", count=1, config=cfg, source="auto")

    assert jobs
    assert jobs[0].source == "liepin_mcp"
    tavily.fetch.assert_called_once()
    liepin.fetch.assert_called_once()


def test_search_jobs_listings_do_not_fill_quota():
    from unittest.mock import MagicMock, patch

    cfg = AppConfig(tavily_api_key="t", liepin_mcp_token="tok")
    listings = [
        Job(
            id=f"list-{i}",
            title="Python 后端工程师",
            jd_text="Python FastAPI",
            source="tavily",
            url=f"https://www.zhipin.com/zhaopin/ae7868255d0e25bd{i}",
        )
        for i in range(8)
    ]
    liepin_jobs = [
        Job(
            id="liepin-1",
            title="Python 后端开发",
            jd_text="FastAPI Django",
            source="liepin_mcp",
            url="https://www.liepin.com/job/19123456789",
        )
    ]
    with (
        patch("repo2resume.jobs.search.LiepinMcpSource") as liepin_cls,
        patch("repo2resume.jobs.search.TavilyJobSource") as tavily_cls,
    ):
        tavily = MagicMock()
        tavily.fetch.return_value = listings
        tavily_cls.return_value = tavily
        liepin = MagicMock()
        liepin.fetch.return_value = liepin_jobs
        liepin_cls.return_value = liepin
        jobs = search_jobs("Python 后端工程师 上海", count=8, config=cfg, source="auto")

    tavily.fetch.assert_called_once()
    liepin.fetch.assert_called_once()
    assert any(j.source == "liepin_mcp" for j in jobs)
    assert all("/zhaopin/" not in (j.url or "") for j in jobs)


def test_liepin_token_roundtrip_and_env(data_dir, monkeypatch):
    save_config(AppConfig(data_dir=data_dir, liepin_mcp_token="lp-token"))
    loaded = load_config(data_dir)
    assert loaded.liepin_mcp_token == "lp-token"

    monkeypatch.setenv("LIEPIN_USER_TOKEN", "from-cli-env")
    monkeypatch.delenv("REPO2RESUME_LIEPIN_MCP_TOKEN", raising=False)
    # toml still wins over unprefixed env when pick finds the file key
    loaded_file = load_config(data_dir)
    assert loaded_file.liepin_mcp_token == "lp-token"

    monkeypatch.setenv("REPO2RESUME_LIEPIN_MCP_TOKEN", "from-prefixed-env")
    loaded_env = load_config(data_dir)
    assert loaded_env.liepin_mcp_token == "from-prefixed-env"
