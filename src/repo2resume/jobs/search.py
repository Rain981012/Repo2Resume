"""职位搜索：mock + Tavily（+ 显式 Bocha）+ 猎聘 MCP / 阿里 TOP + SQLite 缓存。

【AI 辅助】JobSource 协议；auto 降级链：Tavily → 猎聘 MCP → Alibaba TOP → mock。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import httpx

from repo2resume.config import AppConfig
from repo2resume.jobs.bigtech import is_official_job_url, official_domains
from repo2resume.jobs.liepin_mcp import DEFAULT_LIEPIN_MCP_URL, LiepinMcpSource
from repo2resume.jobs.relevance import job_matches_query
from repo2resume.jobs.site_rank import is_boss_url, site_score
from repo2resume.storage.db import Database
from repo2resume.storage.models import (
    EligibilityFlags,
    Job,
    JobCandidate,
    JobContent,
    JobDiscovery,
    JobVerification,
    SalaryInfo,
    SearchRun,
    candidate_to_job_projection,
)

logger = logging.getLogger(__name__)

JOB_CACHE_TTL_HOURS = 24
JOB_SEARCH_LOOKBACK_DAYS = 182  # 约 6 个月；Tavily 无 month×6 档，用起止日
BOCHA_WEB_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
ALIBABA_TOP_URL = "https://gw.api.taobao.com/router/rest"

# db 为 None（纯内存跑）时的占位；有 db 时统一用 Database.lock，避免两把锁互不可见
_NO_DB_LOCK = threading.RLock()


def _db_guard(db: Database | None) -> AbstractContextManager[Any]:
    """直接操作 `db.conn` 的代码块必须持有 Database 自己的锁。"""
    return _NO_DB_LOCK if db is None else db.lock

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

    def fetch(self, query: str, count: int) -> list[Job]: ...


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
        if jobs is None:
            self.jobs = [Job.model_validate(j) for j in _DEFAULT_MOCK_JOBS]
        else:
            self.jobs = list(jobs)

    def fetch(self, query: str, count: int) -> list[Job]:
        if not query.strip():
            return []
        tokens = _expand_query_tokens(query)

        def score(job: Job) -> int:
            text = f"{job.title} {job.company or ''} {' '.join(job.skills)} {job.jd_text}".lower()
            return sum(1 for t in tokens if t in text)

        scored = sorted(self.jobs, key=score, reverse=True)
        positive = [j for j in scored if score(j) > 0]
        return (positive or scored)[:count]


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def _job_search_window() -> tuple[str, str]:
    """返回 (start_date, end_date) ISO 日期，窗口约 6 个月。"""
    end = datetime.now(UTC).date()
    start = end - timedelta(days=JOB_SEARCH_LOOKBACK_DAYS)
    return start.isoformat(), end.isoformat()

# 大厂校招官网。这些站基本不设人机验证，判活和详情解析都比 Boss/智联顺利。
_CAMPUS_DOMAINS = official_domains()

# 中文招聘站域名池（Tavily include_domains；过严时空结果会去掉限制重试一次）
# 注意：仅限域名不够——zhipin.com 常返回 /zhaopin/ 搜索页，必须再用 URL 分类过滤。
_ZH_JOB_DOMAINS = [
    "zhipin.com",
    "www.zhipin.com",
    "51job.com",
    "www.51job.com",
    "jobs.51job.com",
    "liepin.com",
    "www.liepin.com",
    "zhaopin.com",
    "www.zhaopin.com",
    "lagou.com",
    "www.lagou.com",
    "nowcoder.com",
    "www.nowcoder.com",
    "talent.alibaba.com",
    "job.alibaba.com",
    "shixiseng.com",
    "www.shixiseng.com",
    *_CAMPUS_DOMAINS,
]

# 进程内记住鉴权失败的源，避免同一次 chat 里对 401 反复重试
_AUTH_FAILED_SOURCES: set[str] = set()

# 标题层只挡明显的聚合页。不要用「招聘信息」：Boss 详情页被搜索引擎收录时标题经常带这四个字，
# URL 门禁已经拒绝 /zhaopin/ 搜索页。
_LISTING_TITLE_MARKERS = (
    "jobs in",
    "job board",
    "招聘首页",
    "职位列表",
    "search results",
)
_LISTING_COUNT_TITLE_RE = re.compile(r"\d+\s*(?:个|条)职位")
# 搜索引擎常把薪酬 SEO 页、校招活动页收成 job_detail
_UNUSABLE_TITLE_MARKERS = ("工资待遇", "校聘活动", "请稍候", "职场文库", "名企招聘信息")
_UNUSABLE_SNIPPET_MARKERS = ("登录注册后可以",)
_STALE_CAMPUS_YEAR_RE = re.compile(r"(20\d{2})")

# 明确「非详情页」：搜索/列表/中间页
_REJECT_URL_MARKERS = (
    "/zhaopin/",  # BOSS 关键词搜索页，不是 job_detail
    "/web/passport/",
    "security.html",
    "msearch.",
    "/search?",
    "/web/geek/job?",
    "yupao.com/y/",
    "/joblist",
    "/jobs/list",
    "/c/joblist",
    "campus.51job.com",
    "/positions.html",
    # 只挡 51job 的校招活动页；大厂校招官网的详情路径本来就带 /campus/
    "51job.com/campus/",
    "javascript:",
    "mwenku.",
)

# 可接受的「岗位详情」URL 形态（硬门禁）
_DETAIL_URL_RES = (
    re.compile(r"zhipin\.com/job_detail/[A-Za-z0-9_-]+", re.I),
    re.compile(r"liepin\.com/(?:job|a)/\d+", re.I),
    re.compile(r"jobs\.51job\.com/[^?\s]+/\d+\.html", re.I),
    re.compile(r"zhaopin\.com/jobdetail/[A-Za-z0-9]+\.htm", re.I),
    re.compile(r"jobs\.zhaopin\.com/[A-Za-z0-9]+\.htm", re.I),
    re.compile(r"lagou\.com/jobs/\d+", re.I),
    re.compile(r"nowcoder\.com/.+(?:job|position)", re.I),
    re.compile(r"shixiseng\.com/intern/[A-Za-z0-9_-]+", re.I),
    # 大厂校招常见详情
    re.compile(r"jobs\.bytedance\.com/.+/position/\d+", re.I),
    re.compile(r"jobs\.bytedance\.com/.+", re.I),
    re.compile(r"careers\.tencent\.com/.*jobdesc", re.I),
    re.compile(r"careers\.tencent\.com/.+", re.I),
    re.compile(r"campus-talent\.alibaba\.com/.+", re.I),
    re.compile(r"talent\.alibaba\.com/.+", re.I),
    re.compile(r"job\.alibaba\.com/.+", re.I),
    re.compile(r"talent\.antgroup\.com/.+", re.I),
    re.compile(r"campushr\.hikvision\.com/.+", re.I),
    re.compile(r"campus\.kuaishou\.cn/.+", re.I),
    re.compile(r"jobs\.mihoyo\.com/.+", re.I),
    re.compile(r"zhaopin\.jd\.com/.+", re.I),
    re.compile(r"campus\.hr\.xiaomi\.com/.+", re.I),
    re.compile(r"nio\.jobs\.feishu\.cn/.+", re.I),
    re.compile(r"xiaopeng\.jobs\.feishu\.cn/.+", re.I),
    re.compile(r"campus\.dewu\.com/.+", re.I),
    re.compile(r"zhaopin\.meituan\.com/.*(?:job|position)", re.I),
    re.compile(r"campus\.meituan\.com/.+", re.I),
    re.compile(r"hr\.163\.com/job|hr\.163\.com/position", re.I),
    re.compile(r"talent\.jd\.com/.+", re.I),
    re.compile(r"campus\.jd\.com/.+", re.I),
    re.compile(r"(?:hire|careers)\.xiaomi\.com/.+", re.I),
    re.compile(r"careers\.pinduoduo\.com/.+", re.I),
    re.compile(r"jobs\.bilibili\.com/.+", re.I),
    re.compile(r"talent\.didiglobal\.com/.+", re.I),
    re.compile(r"career\.huawei\.com/.+", re.I),
    re.compile(r"app\.mokahr\.com/.*(?:campus-recruitment|social-recruitment)/.+", re.I),
)

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _classify_url_confidence(url: str | None) -> tuple[str, str]:
    """返回 (url_bucket, confidence)。门禁改为分类，不在此硬过滤。"""
    u = _normalize_job_url(url or "")
    if not u.startswith("http"):
        return "invalid", "low"
    lower = u.lower()
    if _is_job_detail_url(u):
        return "job_detail", "high"
    if any(bad in lower for bad in _REJECT_URL_MARKERS):
        return "search_or_list", "low"
    if any(token in lower for token in ("/job", "/career", "/position", "/opportunity")):
        return "company_or_generic", "medium"
    return "unknown", "low"


def _counts_toward_quota(job: Job) -> bool:
    """搜索列表页 / 文库等 unknown 联网结果不占预算；mock 与详情/公司岗才算。"""
    if (job.source or "") == "mock":
        return True
    url = (job.url or "").strip()
    if not url:
        return True
    bucket, _ = _classify_url_confidence(job.url)
    return bucket in {"job_detail", "company_or_generic"}


def _estimate_completeness(jd_text: str, *, confidence: str, url: str | None = None) -> float:
    text = (jd_text or "").strip()
    if not text:
        return 0.0
    base = 0.25
    if confidence == "high":
        base = 0.65
    elif confidence == "medium":
        base = 0.45
    length_bonus = min(len(text) / 1200.0, 0.35)
    return max(0.0, min(1.0, base + length_bonus))


def _job_to_candidate(job: Job, *, run_id: str, query: str) -> JobCandidate:
    bucket, conf = _classify_url_confidence(job.url)
    discovered_at = job.discovered_at or _now_iso()
    verification = JobVerification(
        status=job.verification_status if job.verification_status in {"live", "offline", "blocked"} else "unverified",
        method="search_snippet" if is_boss_url(job.url) else "url_classifier",
        checked_at=job.last_verified_at,
    )
    salary = SalaryInfo(raw=None, confidence=0.0)
    return JobCandidate(
        candidate_id=job.id,
        run_id=run_id,
        source=job.source,
        url=job.url,
        confidence=conf,  # type: ignore[arg-type]
        discovery=JobDiscovery(
            title=job.title,
            snippet=(job.jd_text or "")[:280],
            discovered_at=discovered_at,
            search_query=query,
        ),
        verification=verification,
        content=JobContent(
            jd_text=job.jd_text or "",
            completeness=_estimate_completeness(job.jd_text or "", confidence=conf, url=job.url),
            salary=salary,
        ),
        eligibility=EligibilityFlags(),
        posted_at=job.posted_at,
        posted_at_source=None,
        last_seen_at=job.last_seen_at or discovered_at,
        last_verified_at=job.last_verified_at,
        metadata={
            "company": job.company,
            "location": job.location,
            "skills": job.skills,
            "url_bucket": bucket,
        },
    )


def _rank_candidates(candidates: list[JobCandidate]) -> list[JobCandidate]:
    return sorted(
        candidates,
        key=lambda c: (
            _CONFIDENCE_RANK.get(c.confidence, 0),
            site_score(c.url),
            c.content.completeness,
        ),
        reverse=True,
    )


def _pick_detail_first(candidates: list[JobCandidate], *, count: int) -> list[JobCandidate]:
    """输出阶段：详情页优先，其次公司页；搜索列表页不进结果。"""
    if not candidates:
        return []
    detail: list[JobCandidate] = []
    medium: list[JobCandidate] = []
    other: list[JobCandidate] = []
    for c in candidates:
        bucket = str((c.metadata or {}).get("url_bucket") or "")
        if bucket == "search_or_list":
            continue
        if bucket == "job_detail":
            detail.append(c)
        elif c.confidence == "medium" or bucket == "company_or_generic":
            medium.append(c)
        else:
            other.append(c)
    ordered = detail + medium + other
    return ordered[:count]


def _supplement_low_confidence(candidates: list[JobCandidate]) -> list[JobCandidate]:
    """Stage C：对非高置信候选做轻量补证；offline/blocked 直接淘汰。"""
    if not candidates:
        return []
    from repo2resume.jobs.liveness import probe_listing

    out: list[JobCandidate] = []
    for c in candidates:
        if c.confidence == "high" or not c.url or is_boss_url(c.url):
            out.append(c)
            continue
        status = probe_listing(c.url, timeout_s=5.0)
        if status in {"offline", "blocked"}:
            continue
        if status == "live":
            out.append(
                c.model_copy(
                    update={
                        "confidence": "medium",
                        "verification": JobVerification(
                            status="live",
                            method="liveness_probe",
                            checked_at=_now_iso(),
                        ),
                        "last_verified_at": _now_iso(),
                    }
                )
            )
        else:
            out.append(
                c.model_copy(
                    update={
                        "verification": JobVerification(
                            status=status if status in {"blocked", "unknown"} else "unknown",
                            method="liveness_probe",
                            checked_at=_now_iso(),
                        )
                    }
                )
            )
    return out


class TavilyJobSource:
    """Tavily Search → Job 列表（默认走 httpx REST，偏中文招聘站）。"""

    def __init__(
        self,
        api_key: str | None,
        *,
        client: Any | None = None,
        http_client: httpx.Client | None = None,
        prefer_zh: bool = True,
    ) -> None:
        self.api_key = api_key
        self._client = client  # 可选：注入带 .search() 的 SDK mock
        self._http = http_client
        self.prefer_zh = prefer_zh

    def fetch(self, query: str, count: int) -> list[Job]:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.jobs.usage import timed_search_call

        if not self.api_key:
            logger.warning("Tavily API key not configured; skipping Tavily search")
            return []
        if "tavily" in _AUTH_FAILED_SOURCES:
            emit_progress("跳过 Tavily（本进程内先前鉴权失败）")
            return []

        # 多路专搜详情页路径，避免只命中 /zhaopin/ 搜索页；并发请求后合并去重
        detail_queries = _detail_oriented_queries(query, locale="zh-CN" if self.prefer_zh else "en")
        emit_progress(f"Tavily 请求中（详情页导向，并发 {len(detail_queries)} 路）…")
        with timed_search_call("tavily", credits=0.0) as timer:
            credits_used = 0.0
            try:
                raw_items: list[dict[str, Any]] = []
                jobs: list[Job] = []
                seen_url: set[str] = set()

                def _ingest(chunk: list[Any]) -> None:
                    for item in chunk:
                        if not isinstance(item, dict):
                            continue
                        job = _web_hit_to_job(
                            title=str(item.get("title") or ""),
                            url=str(item.get("url") or ""),
                            jd=str(item.get("content") or item.get("raw_content") or ""),
                            company=None,
                            source="tavily",
                            id_prefix="tavily",
                        )
                        if job is None or not job.url or job.url in seen_url:
                            continue
                        # Phase refactor: 不再以 URL 详情规则做硬门禁；统一先入候选，再做分级。
                        if _is_unusable_hit(job):
                            continue
                        seen_url.add(job.url)
                        jobs.append(job)

                def _one_query(dq: str) -> list[Any]:
                    def _run(*, use_dates: bool, domains: list[str] | None) -> list[Any]:
                        if self._client is not None:
                            kwargs: dict[str, Any] = {
                                "query": dq,
                                "max_results": 20,
                                "search_depth": "basic",
                                "include_answer": False,
                            }
                            if use_dates:
                                start, end = _job_search_window()
                                kwargs["start_date"] = start
                                kwargs["end_date"] = end
                            raw = self._client.search(**kwargs)
                            chunk = (raw.get("results") or []) if isinstance(raw, dict) else []
                        else:
                            raw = self._search_http(
                                dq,
                                count,
                                include_domains=domains,
                                use_dates=use_dates,
                            )
                            chunk = raw.get("results") or []
                        return chunk if isinstance(chunk, list) else []

                    domains = None if self._client is not None else _domains_for_query(dq)
                    try:
                        return _run(use_dates=True, domains=domains)
                    except Exception as exc:  # noqa: BLE001
                        msg = str(exc)
                        if "401" in msg or "unauthorized" in msg.lower():
                            raise
                        if not _is_tavily_bad_param(exc):
                            logger.warning("Tavily query skipped: %s", exc)
                            emit_progress(f"Tavily 该路跳过：{dq[:36]}")
                            return []
                        emit_progress(f"Tavily 参数被拒，去掉日期/域名再搜：{dq[:36]}")
                        try:
                            return _run(use_dates=False, domains=None)
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning("Tavily retry skipped: %s", exc2)
                            emit_progress(f"Tavily 该路跳过：{dq[:36]}")
                            return []

                workers = min(4, len(detail_queries)) or 1
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(contextvars.copy_context().run, _one_query, dq)
                        for dq in detail_queries
                    ]
                    for fut in as_completed(futures):
                        try:
                            chunk = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            if "401" in str(exc) or "unauthorized" in str(exc).lower():
                                raise
                            logger.warning("Tavily 一路异常，跳过：%s", exc)
                            emit_progress(f"Tavily 该路跳过：{str(exc)[:80]}")
                            continue
                        credits_used += 1.0
                        raw_items.extend(x for x in chunk if isinstance(x, dict))
                        _ingest(chunk)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                logger.warning("Tavily search failed: %s", exc)
                if "401" in msg or "Unauthorized" in msg or "invalid api key" in msg.lower():
                    _AUTH_FAILED_SOURCES.add("tavily")
                    emit_progress(
                        "Tavily 鉴权失败(401)：请检查 REPO2RESUME_TAVILY_API_KEY / init 配置"
                    )
                    timer.credits = credits_used or 1.0
                    timer.finish(ok=False, note="401")
                else:
                    emit_progress(f"Tavily 请求失败：{exc}")
                    timer.credits = credits_used or 1.0
                    timer.finish(ok=False, note=msg[:80])
                return []

            emit_progress(
                f"Tavily 原始 {len(raw_items)} 条 → 候选 {len(jobs)} 条"
                f"（credits≈{credits_used:.0f}）"
            )
            timer.credits = credits_used or 1.0
            from repo2resume.jobs.site_rank import sort_jobs_by_site

            ranked = sort_jobs_by_site(jobs)
            timer.finish(ok=True, results=len(ranked[:count]))
            return ranked[:count]

    def _search_http(
        self,
        query: str,
        count: int,
        *,
        include_domains: list[str] | None = None,
        use_dates: bool = True,
    ) -> dict[str, Any]:
        del count

        def _post(domains: list[str] | None, *, dates: bool) -> httpx.Response:
            payload: dict[str, Any] = {
                "api_key": self.api_key,
                "query": query,
                "max_results": 20,
                "search_depth": "basic",
                "include_answer": False,
            }
            if dates:
                start, end = _job_search_window()
                payload["start_date"] = start
                payload["end_date"] = end
            if domains:
                payload["include_domains"] = list(domains)
            if self._http is not None:
                return self._http.post(TAVILY_SEARCH_URL, json=payload, timeout=30.0)
            with httpx.Client(timeout=30.0) as client:
                return client.post(TAVILY_SEARCH_URL, json=payload)

        def _raise_if_bad(resp: httpx.Response) -> None:
            if resp.status_code == 401:
                _AUTH_FAILED_SOURCES.add("tavily")
                resp.raise_for_status()
            if resp.status_code == 400:
                raise RuntimeError(resp.text or "bad parameter or other API misuse")
            resp.raise_for_status()

        def _post_or_empty(domains: list[str] | None, *, dates: bool) -> dict[str, Any]:
            resp = _post(domains, dates=dates)
            if resp.status_code == 400:
                return {}
            _raise_if_bad(resp)
            data = resp.json()
            return data if isinstance(data, dict) else {}

        first_domains = include_domains
        resp = _post(first_domains, dates=use_dates)
        if resp.status_code == 400:
            from repo2resume.agent.progress import emit_progress

            emit_progress("Tavily 参数被拒，去掉日期/域名再搜…")
            data = _post_or_empty(None, dates=False)
            return data
        _raise_if_bad(resp)
        data = resp.json()
        if not isinstance(data, dict):
            data = {}
        results = data.get("results")
        if first_domains and (not results) and self._http is None:
            from repo2resume.agent.progress import emit_progress

            emit_progress("Tavily 限定域名无结果，放宽再搜…")
            resp2 = _post(None, dates=use_dates)
            if resp2.status_code == 400:
                emit_progress("Tavily 放宽后再被拒，去掉日期再搜…")
                return _post_or_empty(None, dates=False)
            _raise_if_bad(resp2)
            data2 = resp2.json()
            return data2 if isinstance(data2, dict) else {}
        return data


def _is_tavily_bad_param(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 400:
        return True
    text = str(exc).lower()
    return "bad parameter" in text or "api misuse" in text


def _domains_for_query(query: str) -> list[str] | None:
    """site: 专搜时只放行对应域名，避免被无关站点稀释。"""
    q = query or ""
    site_hit = re.search(r"site:([a-z0-9.-]+)", q, flags=re.I)
    if site_hit:
        host = site_hit.group(1).lower().split("/")[0].removeprefix("www.")
        return [host, f"www.{host}"]
    lower = q.lower()
    if "zhipin.com" in lower:
        return ["zhipin.com", "www.zhipin.com"]
    if "liepin.com" in lower:
        return ["liepin.com", "www.liepin.com"]
    if "51job.com" in lower:
        return ["51job.com", "www.51job.com", "jobs.51job.com"]
    if "zhaopin.com" in lower:
        return ["zhaopin.com", "www.zhaopin.com"]
    if "alibaba.com" in lower:
        return ["talent.alibaba.com", "job.alibaba.com"]
    return list(_ZH_JOB_DOMAINS)


def _detail_oriented_queries(query: str, *, locale: str = "zh-CN") -> list[str]:
    """多路查询：先宽召回，再由 URL 分类器分桶。

    查询里已经带 site: 时不要再扩成猎聘/智联，否则大厂官网词会被冲掉。
    """
    q = query.strip() or "Python 后端"
    short = q if len(q) <= 80 else q[:80]
    if "site:" in short.lower():
        return [short]
    if locale.lower().startswith("zh"):
        return [
            f"{short} site:zhipin.com/job_detail",
            f"{short} site:www.liepin.com/job",
            f"{short} site:www.zhaopin.com/jobdetail",
            f"{short} site:jobs.51job.com",
            f"{short} site:talent.alibaba.com",
        ]
    return [
        f"{short} hiring site:boards.greenhouse.io",
        f"{short} job opening hiring",
    ]


def _normalize_job_url(url: str) -> str:
    u = (url or "").strip()
    if "m.zhipin.com" in u:
        u = u.replace("m.zhipin.com", "www.zhipin.com")
    if "m.liepin.com" in u:
        u = u.replace("m.liepin.com", "www.liepin.com")
    return u


_EMPTY_JOB_ID_RE = re.compile(
    r"(?:postid|pid|positionid|jobunionid|id)=(?:&|$)", re.I
)


def _is_job_detail_url(url: str) -> bool:
    """硬门禁：只接受可识别的岗位详情页 URL。"""
    u = _normalize_job_url(url)
    if not u.startswith("http"):
        return False
    lower = u.lower()
    if any(bad in lower for bad in _REJECT_URL_MARKERS):
        return False
    query = u.split("?", 1)[1] if "?" in u else ""
    if query and _EMPTY_JOB_ID_RE.search(query):
        return False
    if is_official_job_url(u):
        return True
    return any(rx.search(u) for rx in _DETAIL_URL_RES)


def _is_listing_aggregate(job: Job) -> bool:
    """丢掉「1585 jobs in …」这类聚合列表页标题。"""
    title = (job.title or "").lower()
    if any(m in title for m in _LISTING_TITLE_MARKERS):
        return True
    if _LISTING_COUNT_TITLE_RE.search(job.title or ""):
        return True
    if "jobs" in title and any(ch.isdigit() for ch in title[:6]):
        return True
    return False


def _is_unusable_hit(job: Job) -> bool:
    """薪酬 SEO 页、校招活动页、登录墙摘要：URL 像详情，但不能当岗位。

    Boss 详情 URL 的 Tavily content 经常夹带「登录注册后可以」，仍按摘要评分，不丢。
    """
    title = job.title or ""
    if any(m in title for m in _UNUSABLE_TITLE_MARKERS):
        return True
    if _is_listing_aggregate(job):
        return True
    if is_boss_url(job.url):
        return False
    url = (job.url or "").lower()
    blob = f"{title}\n{job.jd_text or ''}"
    years = [int(y) for y in _STALE_CAMPUS_YEAR_RE.findall(f"{url}\n{blob[:600]}")]
    if "campus.51job.com" in url and years and max(years) <= datetime.now(UTC).year - 2:
        return True
    return any(m in blob for m in _UNUSABLE_SNIPPET_MARKERS)


def _merge_unique_jobs(existing: list[Job], incoming: list[Job]) -> list[Job]:
    seen = {(j.url or j.id) for j in existing}
    out = list(existing)
    for job in incoming:
        key = job.url or job.id
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def _bocha_queries(query: str) -> list[str]:
    """补搜避开 Boss：匿名抓取会被验证页挡掉。"""
    q = query.strip() or "Python 后端"
    short = q if len(q) <= 80 else q[:80]
    return [
        f"{short} site:www.liepin.com/job",
        f"{short} site:jobs.51job.com",
        f"{short} site:www.zhaopin.com/jobdetail",
    ]


def _jobs_from_bocha_pages(pages: list[dict[str, Any]], *, count: int) -> list[Job]:
    jobs: list[Job] = []
    for page in pages[: max(count * 3, count)]:
        job = _web_hit_to_job(
            title=str(page.get("name") or ""),
            url=str(page.get("url") or ""),
            jd=str(page.get("summary") or page.get("snippet") or ""),
            company=str(page.get("siteName") or "").strip() or None,
            source="bocha",
            id_prefix="bocha",
        )
        if job is None or not job.url:
            continue
        if _is_unusable_hit(job):
            continue
        jobs.append(job)
        if len(jobs) >= count:
            break
    return jobs


class BochaJobSource:
    """博查 Web Search → Job 列表。"""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._client = client

    def fetch(self, query: str, count: int) -> list[Job]:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.jobs.usage import timed_search_call

        if "bocha" in _AUTH_FAILED_SOURCES:
            emit_progress("跳过 Bocha（本进程内先前鉴权失败，请检查 API key）")
            return []

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        jobs: list[Job] = []
        n_calls = 0
        with timed_search_call("bocha", credits=0.0) as timer:
            try:
                for q in _bocha_queries(query):
                    if len(jobs) >= count:
                        break
                    emit_progress(f"Bocha 请求中：{q[:60]}")
                    start, end = _job_search_window()
                    payload = {
                        "query": q,
                        "summary": True,
                        "freshness": f"{start}..{end}",
                        "count": min(max(count, 1), 50),
                    }
                    if self._client is not None:
                        resp = self._client.post(
                            BOCHA_WEB_SEARCH_URL,
                            headers=headers,
                            json=payload,
                            timeout=self.timeout_s,
                        )
                    else:
                        with httpx.Client(timeout=self.timeout_s) as client:
                            resp = client.post(
                                BOCHA_WEB_SEARCH_URL,
                                headers=headers,
                                json=payload,
                            )
                    n_calls += 1
                    if resp.status_code == 401:
                        _AUTH_FAILED_SOURCES.add("bocha")
                        emit_progress(
                            "Bocha 鉴权失败(401)：key 无效或过期。请 repo2resume init 重填，"
                            "或设置 REPO2RESUME_BOCHA_API_KEY"
                        )
                        timer.credits = float(n_calls or 1)
                        timer.finish(ok=False, note="401")
                        return []
                    resp.raise_for_status()
                    data = resp.json()
                    pages = _extract_bocha_pages(data)
                    emit_progress(f"Bocha 返回 {len(pages)} 条网页，过滤非详情 URL…")
                    jobs = _merge_unique_jobs(jobs, _jobs_from_bocha_pages(pages, count=count))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bocha web-search failed: %s", exc)
                if "401" in str(exc):
                    _AUTH_FAILED_SOURCES.add("bocha")
                    emit_progress("Bocha 鉴权失败(401)：请检查 API key")
                    timer.credits = float(n_calls or 1)
                    timer.finish(ok=False, note="401")
                else:
                    emit_progress(f"Bocha 请求失败：{exc}")
                    timer.credits = float(n_calls or 1)
                    timer.finish(ok=False, note=str(exc)[:80])
                return jobs if jobs else []

            emit_progress(f"Bocha 保留详情页 {len(jobs)} 条")
            timer.credits = float(n_calls or 1)
            timer.finish(ok=True, results=len(jobs))
            return jobs[:count]


def _extract_bocha_pages(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    candidates = [
        data.get("webPages"),
        (data.get("data") or {}).get("webPages") if isinstance(data.get("data"), dict) else None,
    ]
    for block in candidates:
        if isinstance(block, dict):
            value = block.get("value")
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
    return []


def _top_md5_sign(params: dict[str, str], app_secret: str) -> str:
    joined = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    raw = f"{app_secret}{joined}{app_secret}".encode("utf-8")
    return hashlib.md5(raw).hexdigest().upper()


def _extract_alibaba_positions(data: Any) -> list[dict[str, Any]]:
    """兼容多个 TOP 返回形状，抽职位数组。"""
    if not isinstance(data, dict):
        return []
    root = data.get("alibaba_recruit_website_jobs_search_response")
    if not isinstance(root, dict):
        return []
    result = root.get("result")
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if not isinstance(content, dict):
        return []
    datas = content.get("datas")
    if isinstance(datas, list):
        return [x for x in datas if isinstance(x, dict)]
    if isinstance(datas, dict):
        dto = datas.get("position_dto")
        if isinstance(dto, list):
            return [x for x in dto if isinstance(x, dict)]
        if isinstance(dto, dict):
            return [dto]
    for key in ("data_list", "positions", "records"):
        value = content.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


class AlibabaTopSource:
    """阿里招聘 TOP API 源（需 app_key + app_secret）。"""

    def __init__(self, app_key: str, app_secret: str, *, timeout_s: float = 20.0) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.timeout_s = timeout_s

    def fetch(self, query: str, count: int) -> list[Job]:
        from repo2resume.agent.progress import emit_progress
        from repo2resume.jobs.usage import timed_search_call

        if "alibaba_top" in _AUTH_FAILED_SOURCES:
            emit_progress("跳过阿里 TOP API（本进程内先前鉴权失败）")
            return []

        method = "alibaba.recruit.website.jobs.search"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_query = {
            "keyword": query,
            "page_index": 1,
            "page_size": max(1, min(20, count)),
            "desc": True,
        }
        params: dict[str, str] = {
            "method": method,
            "app_key": self.app_key,
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
            "timestamp": timestamp,
            "partner_id": "repo2resume",
            "query": json.dumps(payload_query, ensure_ascii=False),
        }
        params["sign"] = _top_md5_sign(params, self.app_secret)

        with timed_search_call("alibaba_top", credits=0.0) as timer:
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(ALIBABA_TOP_URL, data=params)
                if resp.status_code == 401:
                    _AUTH_FAILED_SOURCES.add("alibaba_top")
                    timer.finish(ok=False, note="401")
                    emit_progress("阿里 TOP API 鉴权失败(401)：请检查 app_key/app_secret")
                    return []
                resp.raise_for_status()
                data = resp.json()
                positions = _extract_alibaba_positions(data)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "invalid-signature" in msg.lower() or "invalid appkey" in msg.lower():
                    _AUTH_FAILED_SOURCES.add("alibaba_top")
                timer.finish(ok=False, note=msg[:80])
                emit_progress(f"阿里 TOP API 请求失败：{exc}")
                return []

            jobs: list[Job] = []
            for pos in positions[: max(count * 2, count)]:
                code = str(
                    pos.get("code")
                    or pos.get("position_code")
                    or pos.get("positionCode")
                    or ""
                ).strip()
                title = str(pos.get("name") or pos.get("title") or "").strip()
                desc = str(pos.get("description") or pos.get("job_desc") or "").strip()
                req = str(pos.get("requirement") or pos.get("requirement_desc") or "").strip()
                dept = str(pos.get("department_name") or "").strip()
                jd = "\n".join(x for x in (desc, req) if x)
                if not code and not title:
                    continue
                url = f"https://talent.alibaba.com/position/detail?positionCode={code}" if code else None
                job = _web_hit_to_job(
                    title=title or "阿里岗位",
                    url=url or "",
                    jd=jd or title,
                    company=dept or "阿里巴巴",
                    source="alibaba_top",
                    id_prefix="alibaba",
                )
                if job is None:
                    continue
                jobs.append(job)
                if len(jobs) >= count:
                    break
            timer.finish(ok=True, results=len(jobs))
            if jobs:
                emit_progress(f"阿里 TOP API 返回 {len(jobs)} 条职位")
            return jobs


def _web_hit_to_job(
    *,
    title: str,
    url: str,
    jd: str,
    company: str | None,
    source: str,
    id_prefix: str,
) -> Job | None:
    url = _normalize_job_url(url)
    title = (title or "").strip()
    jd = (jd or "").strip()
    if not url and not title and not jd:
        return None
    if not url:
        url = f"{id_prefix}://{hashlib.sha256((title + jd).encode()).hexdigest()[:16]}"
    job_id = f"{id_prefix}-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return Job(
        id=job_id,
        title=title or (url[:80] if url else "未命名职位"),
        company=company,
        location=None,
        jd_text=jd,
        skills=[],
        source=source,
        url=url if url.startswith("http") else None,
    )


def _cache_key(query: str, count: int, source: str) -> str:
    payload = f"{source}:{query}:{count}"
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
    # 同时按 job.id 落单行，供 generate_resume(job_id=bocha-xxxx) 查库
    persist_jobs(db, jobs)


def persist_jobs(db: Database | None, jobs: list[Job]) -> None:
    """把单条职位写入 jobs 表（id=bocha-xxxx / tavily-xxxx），供 job_id 解析。"""
    if db is None or not jobs:
        return
    now = datetime.now(UTC).isoformat()
    for j in jobs:
        skills = json.dumps(j.skills or [], ensure_ascii=False)
        payload = json.dumps(j.model_dump(), ensure_ascii=False)
        db.conn.execute(
            "INSERT INTO jobs(id, title, company, location, jd_text, skills, source, url, "
            "updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, company=excluded.company, "
            "location=excluded.location, jd_text=excluded.jd_text, skills=excluded.skills, "
            "source=excluded.source, url=excluded.url, updated_at=excluded.updated_at, "
            "payload_json=excluded.payload_json",
            (
                j.id,
                j.title,
                j.company or "",
                j.location or "",
                j.jd_text or "",
                skills,
                j.source,
                j.url or "",
                now,
                payload,
            ),
        )
    db.conn.commit()


def _auto_source_chain(config: AppConfig | None) -> list[str]:
    """auto 降级链：Tavily → 猎聘 MCP → Alibaba TOP(可选) → mock。"""
    chain: list[str] = []
    if config and getattr(config, "tavily_api_key", None):
        chain.append("tavily")
    if config and getattr(config, "liepin_mcp_token", None):
        chain.append("liepin_mcp")
    if (
        config
        and getattr(config, "alibaba_top_app_key", None)
        and getattr(config, "alibaba_top_app_secret", None)
    ):
        chain.append("alibaba_top")
    if not chain:
        chain.append("mock")
    return chain


def _make_source(name: str, config: AppConfig | None) -> JobSource:
    if name == "bocha":
        key = getattr(config, "bocha_api_key", None) if config else None
        if not key:
            return MockJobSource([])
        return BochaJobSource(key)
    if name == "tavily":
        key = getattr(config, "tavily_api_key", None) if config else None
        return TavilyJobSource(key)
    if name == "alibaba_top":
        app_key = getattr(config, "alibaba_top_app_key", None) if config else None
        app_secret = getattr(config, "alibaba_top_app_secret", None) if config else None
        if not app_key or not app_secret:
            return MockJobSource([])
        return AlibabaTopSource(app_key, app_secret)
    if name == "liepin_mcp":
        token = getattr(config, "liepin_mcp_token", None) if config else None
        if not token:
            return MockJobSource([])
        url = getattr(config, "liepin_mcp_url", None) if config else None
        return LiepinMcpSource(token, mcp_url=url or DEFAULT_LIEPIN_MCP_URL)
    return MockJobSource()


def search_jobs(
    query: str,
    *,
    count: int = 5,
    config: AppConfig | None = None,
    db: Database | None = None,
    source: str = "auto",
) -> list[Job]:
    """搜索职位；内部落 SearchRun/JobCandidate，外部仍返回兼容 Job。"""
    from repo2resume.agent.progress import emit_progress

    if not query.strip() or count <= 0:
        return []

    requested = (source or "auto").strip().lower()
    if requested == "auto":
        chain = _auto_source_chain(config)
    else:
        chain = [requested]

    live_configured = bool(
        (config and getattr(config, "bocha_api_key", None))
        or (config and getattr(config, "tavily_api_key", None))
        or (config and getattr(config, "liepin_mcp_token", None))
        or (
            config
            and getattr(config, "alibaba_top_app_key", None)
            and getattr(config, "alibaba_top_app_secret", None)
        )
    )

    # 缓存按「最终会用的首选源」键；auto 用整条链签名，避免混源脏读
    cache_tag = requested if requested != "auto" else "auto:" + ">".join(chain)
    #  bump 缓存版本：避免沿用旧 mock / 过窄召回条目
    cache_tag = f"v17:{cache_tag}"
    key = _cache_key(query, count, cache_tag)
    run_id = f"run-{uuid4().hex[:12]}"
    run_started_at = _now_iso()
    if db is not None:
        db.save_search_run(
            SearchRun(
                run_id=run_id,
                prefs_version=1,
                queries=[query],
                sources=list(chain),
                started_at=run_started_at,
            )
        )
    with _db_guard(db):
        cached = _load_cached_jobs(db, key)
    if cached is not None:
        if live_configured and cached and all(j.source == "mock" for j in cached):
            emit_progress("丢弃污染的 mock 缓存，重新联网搜索")
        else:
            emit_progress(f"职位缓存命中（{cache_tag}）")
            from repo2resume.observability.langsmith_span import span_call

            def _cached_jobs() -> list:
                if db is not None:
                    for j in cached:
                        db.upsert_job_candidate(_job_to_candidate(j, run_id=run_id, query=query))
                    db.save_search_run(
                        SearchRun(
                            run_id=run_id,
                            prefs_version=1,
                            queries=[query],
                            sources=list(chain),
                            started_at=run_started_at,
                            completed_at=_now_iso(),
                        )
                    )
                with _db_guard(db):
                    persist_jobs(db, cached)
                return cached

            return span_call(
                "jobs.cache",
                _cached_jobs,
                outputs_of=lambda jobs: {"hit": True, "n": len(jobs)},
            )

    from repo2resume.observability.langsmith_span import span_call as _span_miss

    _span_miss("jobs.cache", lambda: None, outputs_of=lambda _: {"hit": False})
    emit_progress(f"职位搜索链：{' → '.join(chain)} | query={query[:40]}")

    jobs: list[Job] = []
    used_names: list[str] = []
    try:
        for i, name in enumerate(chain):
            if len(jobs) >= count:
                break
            if name == "mock":
                batch = MockJobSource().fetch(query, count)
            else:
                src = _make_source(name, config)
                try:
                    batch = src.fetch(query, count)
                except NotImplementedError:
                    batch = []
                except Exception as exc:  # noqa: BLE001
                    logger.warning("job source %s failed: %s", name, exc)
                    emit_progress(f"{name} 异常：{exc}")
                    batch = []
            kept = [j for j in batch if job_matches_query(j, query)]
            if len(kept) < len(batch):
                emit_progress(f"{name} 相关性过滤 {len(batch)}→{len(kept)}")
            batch = [j for j in kept if _counts_toward_quota(j)]
            if len(batch) < len(kept):
                emit_progress(f"{name} 丢弃搜索列表页 {len(kept) - len(batch)} 条")
            before = len(jobs)
            jobs = _merge_unique_jobs(jobs, batch)
            if len(jobs) > before:
                used_names.append(name)
            if len(jobs) >= count:
                break
            if i < len(chain) - 1:
                emit_progress(f"{name} 可用详情 {len(jobs)}/{count}，尝试 {chain[i + 1]} 补足…")
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_jobs aborted: %s", exc)
        emit_progress(f"本路搜索中断，已跳过：{type(exc).__name__}")

    used = "+".join(used_names) if used_names else chain[0]
    if not jobs and requested == "auto" and "mock" not in chain:
        # 配了联网 key 却召回为空，说明是这组关键词的问题：返回空让上层换词，
        # 塞示例职位只会用无链接的假岗污染候选池。
        if live_configured:
            emit_progress("本组关键词联网无结果（已配置联网源，不降级示例职位）")
        else:
            emit_progress("未配置联网源，降级 mock 示例职位（不写入联网缓存）")
            jobs = MockJobSource().fetch(query, count)
            used = "mock"

    if not jobs:
        if db is not None:
            db.save_search_run(
                SearchRun(
                    run_id=run_id,
                    prefs_version=1,
                    queries=[query],
                    sources=list(chain),
                    started_at=run_started_at,
                    completed_at=_now_iso(),
                )
            )
        return []

    from repo2resume.jobs.site_rank import sort_jobs_by_site

    stage_a = sort_jobs_by_site(jobs)[: max(count * 3, count)]
    candidates = [_job_to_candidate(j, run_id=run_id, query=query) for j in stage_a]
    candidates = _supplement_low_confidence(candidates)
    candidates = [c for c in candidates if c.verification.status != "offline"]
    ranked_candidates = _rank_candidates(candidates)
    selected = _pick_detail_first(ranked_candidates, count=count)
    projected = [candidate_to_job_projection(c) for c in selected]

    if db is not None:
        for c in ranked_candidates:
            db.upsert_job_candidate(c)
        db.save_search_run(
            SearchRun(
                run_id=run_id,
                prefs_version=1,
                queries=[query],
                sources=list(chain),
                started_at=run_started_at,
                completed_at=_now_iso(),
            )
        )

    n_detail = sum(
        1 for c in selected if str((c.metadata or {}).get("url_bucket") or "") == "job_detail"
    )
    emit_progress(
        f"职位搜索完成：source={used}，候选 {len(candidates)} 条，输出 {len(projected)} 条"
        f"（详情页 {n_detail}）"
    )
    # mock 兜底不要写进 auto:tavily>bocha 缓存，否则下次「命中」仍是 StartupXYZ
    with _db_guard(db):
        if used == "mock" and live_configured and requested == "auto":
            persist_jobs(db, projected)
        else:
            _save_cached_jobs(db, key, projected)
    return projected


def explain_job_candidate(db: Database | None, candidate_id: str) -> dict[str, Any] | None:
    """返回候选岗位的可解释来源信息。"""
    if db is None:
        return None
    return db.explain_job_origin(candidate_id)
