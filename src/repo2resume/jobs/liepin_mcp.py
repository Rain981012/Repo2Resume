"""猎聘官方 MCP 职位源（JSON-RPC over Streamable HTTP）。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

import httpx

from repo2resume.storage.models import Job

logger = logging.getLogger(__name__)

DEFAULT_LIEPIN_MCP_URL = "https://open-agent.liepin.com/mcp/user"
LIEPIN_SEARCH_TOOL = "user-search-job"
LIEPIN_SEARCH_TOOL_FALLBACK = "search-jobs"
_PROTOCOL_VERSION = "2025-03-26"

_KNOWN_CITIES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "成都",
    "武汉",
    "西安",
    "苏州",
    "重庆",
    "天津",
    "厦门",
    "长沙",
    "合肥",
    "青岛",
    "大连",
    "宁波",
    "无锡",
    "福州",
    "郑州",
    "济南",
    "沈阳",
    "昆明",
    "珠海",
    "东莞",
    "佛山",
)

_JOB_LIST_KEYS = (
    "jobs",
    "jobList",
    "dataList",
    "data_list",
    "records",
    "items",
    "list",
    "data",
    "result",
    "content",
)

_NEST_KEYS = ("job", "jobCard", "jobInfo", "job_info", "comp", "company", "compInfo")


def split_liepin_query(query: str) -> dict[str, str]:
    """把自由文本拆成 MCP 的 jobName / address。"""
    text = " ".join((query or "").split())
    if not text:
        return {}
    address = ""
    remaining = text
    for city in _KNOWN_CITIES:
        if city in remaining:
            address = city
            remaining = remaining.replace(city, " ")
            break
    for noise in ("校招", "社招", "应届", "远程", "全国", "实习生"):
        remaining = remaining.replace(noise, " ")
    compact = " ".join(remaining.split())
    lower = compact.lower()
    if "后端" in compact or "backend" in lower:
        job_name = "Python后端" if "python" in lower else "后端开发"
    elif "前端" in compact or "frontend" in lower:
        job_name = "前端开发"
    elif "人工智能" in compact or re.search(r"(?:^|\s)ai(?:\s|$)", lower):
        job_name = "AI应用"
    else:
        toks = [t for t in compact.split() if t not in {"工程师"}]
        job_name = " ".join(toks[:2]) if toks else compact or text
    args: dict[str, str] = {"jobName": job_name}
    if address:
        args["address"] = address
    return args


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _flatten_job_card(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    for nest in _NEST_KEYS:
        nested = item.get(nest)
        if isinstance(nested, dict):
            for key, value in nested.items():
                out.setdefault(key, value)
    return out


def _extract_job_dicts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        found: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                found.append(_flatten_job_card(item))
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    continue
                found.extend(_extract_job_dicts(parsed))
        return found
    if isinstance(data, dict):
        for key in _JOB_LIST_KEYS:
            nested = data.get(key)
            found = _extract_job_dicts(nested)
            if found:
                return found
        if _first(data, "jobId", "job_id", "jobName", "title", "jobTitle"):
            return [_flatten_job_card(data)]
    if isinstance(data, str):
        try:
            return _extract_job_dicts(json.loads(data))
        except json.JSONDecodeError:
            return []
    return []


def parse_mcp_http_body(text: str, *, content_type: str = "") -> dict[str, Any]:
    """解析 JSON 或 SSE（Streamable HTTP）响应体。"""
    raw = (text or "").strip()
    ctype = (content_type or "").lower()
    if not raw:
        return {}
    looks_sse = "text/event-stream" in ctype or raw.startswith(("event:", "data:"))
    if looks_sse:
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("empty MCP SSE payload")
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("MCP response is not a JSON object")


def unwrap_mcp_tool_result(payload: dict[str, Any]) -> Any:
    if "error" in payload and payload.get("error"):
        err = payload["error"]
        if isinstance(err, dict):
            code = err.get("code", "?")
            message = err.get("message", "未知错误")
            raise RuntimeError(f"MCP error [{code}]: {message}")
        raise RuntimeError(f"MCP error: {err}")
    result = payload.get("result", payload)
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        texts: list[str] = []
        for item in result["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        blob = "\n".join(texts).strip()
        if not blob:
            return result
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return blob
    return result


def _job_url(raw: dict[str, Any], job_id: str) -> str:
    url = str(
        _first(
            raw,
            "jobDetailUrl",
            "job_detail_url",
            "url",
            "jobUrl",
            "job_url",
            "pcUrl",
            "pc_url",
            "detailUrl",
            "link",
        )
        or ""
    ).strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    digits = re.sub(r"\D", "", job_id) if job_id else ""
    if digits:
        return f"https://www.liepin.com/job/{digits}"
    if job_id:
        return f"https://www.liepin.com/job/{job_id}"
    return ""


def jobs_from_mcp_payload(payload: Any, *, count: int) -> list[Job]:
    """把 MCP tools/call 结果映射成 Job。"""
    data = (
        unwrap_mcp_tool_result(payload)
        if isinstance(payload, dict)
        and ("result" in payload or "error" in payload or "content" in payload)
        else payload
    )
    jobs: list[Job] = []
    seen: set[str] = set()
    for raw in _extract_job_dicts(data):
        job_id = str(
            _first(raw, "jobId", "job_id", "ejobId", "ejob_id", "id", "positionId") or ""
        ).strip()
        title = str(
            _first(raw, "jobName", "job_name", "title", "jobTitle", "job_title", "name") or ""
        ).strip()
        company = (
            str(
                _first(raw, "companyName", "company_name", "compName", "comp_name", "company") or ""
            ).strip()
            or None
        )
        location = (
            str(
                _first(
                    raw,
                    "location",
                    "address",
                    "city",
                    "dq",
                    "dqName",
                    "dq_name",
                    "jobDq",
                    "workPlace",
                )
                or ""
            ).strip()
            or None
        )
        salary = str(
            _first(raw, "salary", "salaryShow", "salary_show", "salaryDesc", "compensation") or ""
        ).strip()
        desc = str(
            _first(
                raw,
                "jobDesc",
                "job_desc",
                "description",
                "requirement",
                "jobDuty",
                "detail",
            )
            or ""
        ).strip()
        extra = []
        edu = str(_first(raw, "education", "eduLevel") or "").strip()
        years = str(_first(raw, "workYears", "workExperience") or "").strip()
        industry = str(_first(raw, "industry") or "").strip()
        if edu:
            extra.append(f"学历：{edu}")
        if years:
            extra.append(f"经验：{years}")
        if industry:
            extra.append(f"行业：{industry}")
        url = _job_url(raw, job_id)
        if not title and not url:
            continue
        parts = [
            p
            for p in (
                f"薪资：{salary}" if salary else "",
                *extra,
                desc or title,
            )
            if p
        ]
        jd = "\n".join(parts)
        if not url:
            url = "liepin://" + hashlib.sha256((title + jd).encode()).hexdigest()[:16]
        key = url if url.startswith("http") else title + jd
        if key in seen:
            continue
        seen.add(key)
        jobs.append(
            Job(
                id="liepin-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
                title=title or "猎聘职位",
                company=company,
                location=location,
                jd_text=jd,
                skills=[],
                source="liepin_mcp",
                url=url if url.startswith("http") else None,
            )
        )
        if len(jobs) >= count:
            break
    return jobs


def _is_unknown_tool_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "unknown tool" in msg or "not found" in msg or "未知工具" in msg


def _is_auth_error(exc: BaseException, status_code: int | None = None) -> bool:
    if status_code in {401, 403}:
        return True
    msg = str(exc).lower()
    tokens = ("401", "403", "unauthorized", "unauth", "token", "未授权", "登录", "凭证")
    return any(t in msg for t in tokens)


class LiepinMcpSource:
    """猎聘官方 MCP：initialize + tools/call user-search-job。"""

    def __init__(
        self,
        user_token: str,
        *,
        mcp_url: str | None = None,
        timeout_s: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.user_token = user_token
        self.mcp_url = (mcp_url or DEFAULT_LIEPIN_MCP_URL).rstrip("/")
        self.timeout_s = timeout_s
        self._http = http_client
        self._session_id: str | None = None

    def fetch(self, query: str, count: int) -> list[Job]:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.jobs.search import _AUTH_FAILED_SOURCES
        from repo2resume.jobs.usage import timed_search_call

        if not self.user_token:
            return []
        if "liepin_mcp" in _AUTH_FAILED_SOURCES:
            emit_progress("跳过猎聘 MCP（本进程内先前鉴权失败）")
            return []

        arguments = split_liepin_query(query)
        if not arguments:
            return []

        with timed_search_call("liepin_mcp", credits=0.0) as timer:
            try:
                jobs = self._search(arguments, max(count * 3, count))
            except Exception as exc:  # noqa: BLE001
                if _is_auth_error(exc):
                    _AUTH_FAILED_SOURCES.add("liepin_mcp")
                    timer.finish(ok=False, note="401")
                    emit_progress(
                        "猎聘 MCP 鉴权失败：请到 https://www.liepin.com/mcp/server 重新生成 Token"
                    )
                    return []
                timer.finish(ok=False, note=str(exc)[:80])
                emit_progress(f"猎聘 MCP 请求失败：{exc}")
                return []
            from repo2resume.jobs.relevance import job_matches_query

            matched = [j for j in jobs if job_matches_query(j, query)]
            timer.finish(ok=True, results=len(matched))
            if jobs and not matched:
                emit_progress(f"猎聘 MCP 返回 {len(jobs)} 条但与「{query[:24]}」无关，已丢弃")
            elif matched:
                emit_progress(f"猎聘 MCP 相关职位 {len(matched)} 条")
            return matched[:count]

    def _search(self, arguments: dict[str, str], count: int) -> list[Job]:
        own_client = self._http is None
        client = self._http or httpx.Client(timeout=self.timeout_s)
        try:
            self._initialize(client)
            payload = self._call_tool(client, LIEPIN_SEARCH_TOOL, arguments)
            try:
                return jobs_from_mcp_payload(payload, count=count)
            except RuntimeError as exc:
                if not _is_unknown_tool_error(exc):
                    raise
                payload = self._call_tool(client, LIEPIN_SEARCH_TOOL_FALLBACK, arguments)
                return jobs_from_mcp_payload(payload, count=count)
        finally:
            if own_client:
                client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-user-token": self.user_token,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _post(self, client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
        resp = client.post(self.mcp_url, headers=self._headers(), json=payload)
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if resp.status_code in {401, 403}:
            raise RuntimeError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return parse_mcp_http_body(resp.text, content_type=resp.headers.get("content-type", ""))

    def _initialize(self, client: httpx.Client) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "repo2resume", "version": "0.1.0"},
            },
        }
        result = self._post(client, payload)
        if result.get("error"):
            unwrap_mcp_tool_result(result)

    def _call_tool(
        self, client: httpx.Client, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return self._post(client, payload)
