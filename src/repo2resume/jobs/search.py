"""职位搜索：mock + Tavily + Bocha + SQLite 缓存。

【AI 辅助】JobSource 协议；auto 降级链：Tavily → Bocha → mock。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

from repo2resume.config import AppConfig
from repo2resume.storage.db import Database
from repo2resume.storage.models import Job

logger = logging.getLogger(__name__)

JOB_CACHE_TTL_HOURS = 24
BOCHA_WEB_SEARCH_URL = "https://api.bochaai.com/v1/web-search"

# 多关键词 / 多线程搜岗时保护 SQLite 读写
_DB_LOCK = threading.Lock()

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

# 中文招聘站偏好（Tavily include_domains；过严时空结果会去掉限制重试一次）
# 注意：仅限域名不够——zhipin.com 常返回 /zhaopin/ 搜索页，必须再用详情 URL 规则过滤。
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
]

# 进程内记住鉴权失败的源，避免同一次 chat 里对 401 反复重试
_AUTH_FAILED_SOURCES: set[str] = set()

_LISTING_TITLE_MARKERS = (
    "jobs in",
    "job board",
    "招聘首页",
    "职位列表",
    "search results",
    "个职位",
    "条职位",
    "招聘信息",  # Boss /zhaopin/ 关键词聚合页常见标题
)

# 明确「非详情页」：搜索/列表/中间页
_REJECT_URL_MARKERS = (
    "/zhaopin/",  # BOSS 关键词搜索页，不是 job_detail
    "msearch.",
    "/search?",
    "/web/geek/job?",
    "yupao.com/y/",
    "/joblist",
    "/jobs/list",
    "/c/joblist",
    "javascript:",
)

# 可接受的「岗位详情」URL 形态（硬门禁）
_DETAIL_URL_RES = (
    re.compile(r"zhipin\.com/job_detail/[A-Za-z0-9_-]+", re.I),
    re.compile(r"liepin\.com/job/\d+", re.I),
    re.compile(r"jobs\.51job\.com/[^?\s]+/\d+\.html", re.I),
    re.compile(r"zhaopin\.com/jobdetail/[A-Za-z0-9]+\.htm", re.I),
    re.compile(r"lagou\.com/jobs/\d+", re.I),
    re.compile(r"nowcoder\.com/.+(?:job|position)", re.I),
    # 大厂校招常见详情
    re.compile(r"jobs\.bytedance\.com/.+", re.I),
    re.compile(r"careers\.tencent\.com/.+", re.I),
)


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
                        if not _is_job_detail_url(job.url) or _is_listing_aggregate(job):
                            continue
                        seen_url.add(job.url)
                        jobs.append(job)

                def _one_query(dq: str) -> list[Any]:
                    if self._client is not None:
                        raw = self._client.search(
                            query=dq,
                            max_results=min(max(count, 1), 20),
                            search_depth="basic",
                            include_answer=False,
                        )
                        chunk = (raw.get("results") or []) if isinstance(raw, dict) else []
                    else:
                        raw = self._search_http(dq, count, prefer_zh=False)
                        chunk = raw.get("results") or []
                    return chunk if isinstance(chunk, list) else []

                workers = min(4, len(detail_queries)) or 1
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(contextvars.copy_context().run, _one_query, dq)
                        for dq in detail_queries
                    ]
                    for fut in as_completed(futures):
                        chunk = fut.result()
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
                f"Tavily 原始 {len(raw_items)} 条 → 详情页 {len(jobs)} 条"
                f"（credits≈{credits_used:.0f}）"
            )
            timer.credits = credits_used or 1.0
            timer.finish(ok=True, results=len(jobs[:count]))
            return jobs[:count]

    def _search_http(self, query: str, count: int, *, prefer_zh: bool) -> dict[str, Any]:
        def _post(with_domains: bool) -> httpx.Response:
            payload: dict[str, Any] = {
                "api_key": self.api_key,
                "query": query,
                "max_results": min(max(count, 1), 20),
                "search_depth": "basic",
                "include_answer": False,
            }
            if with_domains and prefer_zh:
                payload["include_domains"] = list(_ZH_JOB_DOMAINS)
            if self._http is not None:
                return self._http.post(TAVILY_SEARCH_URL, json=payload, timeout=30.0)
            with httpx.Client(timeout=30.0) as client:
                return client.post(TAVILY_SEARCH_URL, json=payload)

        resp = _post(with_domains=True)
        if resp.status_code == 401:
            _AUTH_FAILED_SOURCES.add("tavily")
            resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            data = {}
        results = data.get("results")
        if prefer_zh and (not results) and self._http is None:
            from repo2resume.agent.progress import emit_progress

            emit_progress("Tavily 中文站无结果，放宽域名再搜…")
            resp2 = _post(with_domains=False)
            resp2.raise_for_status()
            data2 = resp2.json()
            return data2 if isinstance(data2, dict) else {}
        return data


def _rewrite_job_query(query: str, *, locale: str = "zh-CN") -> str:
    """单路改写（Bocha 等）；默认偏中文 + 详情页词。"""
    q = query.strip()
    if not q:
        return q
    if locale.lower().startswith("zh"):
        if "job_detail" not in q and "职位详情" not in q:
            return f"{q} 职位详情 job_detail 招聘"
        return q
    if "job detail" not in q.lower() and "job opening" not in q.lower():
        return f"{q} job detail opening hiring"
    return q


def _detail_oriented_queries(query: str, *, locale: str = "zh-CN") -> list[str]:
    """多路查询，强制落到各站详情 URL 形态。"""
    q = query.strip() or "Python 后端"
    # 去掉用户可能带的过长画像，控制长度
    short = q if len(q) <= 40 else q[:40]
    if locale.lower().startswith("zh"):
        return [
            f"{short} site:zhipin.com/job_detail",
            f"{short} site:www.liepin.com/job",
            f"{short} site:jobs.51job.com",
            f"{short} site:www.zhaopin.com/jobdetail",
        ]
    return [
        f"{short} job detail site:boards.greenhouse.io",
        f"{short} job opening hiring",
    ]


def _normalize_job_url(url: str) -> str:
    u = (url or "").strip()
    if "m.zhipin.com" in u:
        u = u.replace("m.zhipin.com", "www.zhipin.com")
    if "m.liepin.com" in u:
        u = u.replace("m.liepin.com", "www.liepin.com")
    return u


def _is_job_detail_url(url: str) -> bool:
    """硬门禁：只接受可识别的岗位详情页 URL。"""
    u = _normalize_job_url(url)
    if not u.startswith("http"):
        return False
    lower = u.lower()
    if any(bad in lower for bad in _REJECT_URL_MARKERS):
        return False
    return any(rx.search(u) for rx in _DETAIL_URL_RES)


def _is_listing_aggregate(job: Job) -> bool:
    """丢掉「1585 jobs in …」/「xxx招聘信息」聚合列表页标题。"""
    title = (job.title or "").lower()
    if any(m in title for m in _LISTING_TITLE_MARKERS):
        return True
    if "jobs" in title and any(ch.isdigit() for ch in title[:6]):
        return True
    return False


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

        rewritten = _rewrite_job_query(query, locale="zh-CN")
        # Bocha 单请求：把详情路径写进 query，提高 job_detail 命中率
        if "job_detail" not in rewritten:
            rewritten = (
                f"{rewritten} site:zhipin.com/job_detail"
                " OR site:liepin.com/job OR site:jobs.51job.com"
            )
        payload = {
            "query": rewritten,
            "summary": True,
            "freshness": "oneMonth",
            "count": min(max(count, 1), 50),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        emit_progress(f"Bocha 请求中：{rewritten[:60]}")
        if "bocha" in _AUTH_FAILED_SOURCES:
            emit_progress("跳过 Bocha（本进程内先前鉴权失败，请检查 API key）")
            return []
        with timed_search_call("bocha", credits=1.0) as timer:
            try:
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
                if resp.status_code == 401:
                    _AUTH_FAILED_SOURCES.add("bocha")
                    emit_progress(
                        "Bocha 鉴权失败(401)：key 无效或过期。请 repo2resume init 重填，"
                        "或设置 REPO2RESUME_BOCHA_API_KEY"
                    )
                    timer.finish(ok=False, note="401")
                    return []
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bocha web-search failed: %s", exc)
                if "401" in str(exc):
                    _AUTH_FAILED_SOURCES.add("bocha")
                    emit_progress("Bocha 鉴权失败(401)：请检查 API key")
                    timer.finish(ok=False, note="401")
                else:
                    emit_progress(f"Bocha 请求失败：{exc}")
                    timer.finish(ok=False, note=str(exc)[:80])
                return []

            pages = _extract_bocha_pages(data)
            emit_progress(f"Bocha 返回 {len(pages)} 条网页，过滤非详情 URL…")
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
                if not _is_job_detail_url(job.url):
                    continue
                if _is_listing_aggregate(job):
                    continue
                jobs.append(job)
                if len(jobs) >= count:
                    break
            emit_progress(f"Bocha 保留详情页 {len(jobs)} 条")
            timer.finish(ok=True, results=len(jobs))
            return jobs


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
    """auto 降级链：Tavily → Bocha → mock。"""
    chain: list[str] = []
    if config and getattr(config, "tavily_api_key", None):
        chain.append("tavily")
    if config and getattr(config, "bocha_api_key", None):
        chain.append("bocha")
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
    return MockJobSource()


def search_jobs(
    query: str,
    *,
    count: int = 5,
    config: AppConfig | None = None,
    db: Database | None = None,
    source: str = "auto",
) -> list[Job]:
    """搜索职位；默认 auto：Tavily → Bocha → mock。默认偏中文招聘站。"""
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
    )

    # 缓存按「最终会用的首选源」键；auto 用整条链签名，避免混源脏读
    cache_tag = requested if requested != "auto" else "auto:" + ">".join(chain)
    #  bump 缓存版本：避免沿用旧 mock 污染条目
    cache_tag = f"v3:{cache_tag}"
    key = _cache_key(query, count, cache_tag)
    with _DB_LOCK:
        cached = _load_cached_jobs(db, key)
    if cached is not None:
        if live_configured and cached and all(j.source == "mock" for j in cached):
            emit_progress("丢弃污染的 mock 缓存，重新联网搜索")
        else:
            emit_progress(f"职位缓存命中（{cache_tag}）")
            with _DB_LOCK:
                persist_jobs(db, cached)
            return cached

    emit_progress(f"职位搜索链：{' → '.join(chain)} | query={query[:40]}")

    jobs: list[Job] = []
    used = chain[0]
    for i, name in enumerate(chain):
        used = name
        if name == "mock":
            jobs = MockJobSource().fetch(query, count)
        else:
            src = _make_source(name, config)
            try:
                jobs = src.fetch(query, count)
            except NotImplementedError:
                jobs = []
            except Exception as exc:  # noqa: BLE001
                logger.warning("job source %s failed: %s", name, exc)
                emit_progress(f"{name} 异常：{exc}")
                jobs = []
        if jobs:
            break
        if i < len(chain) - 1:
            emit_progress(f"{name} 无结果，尝试 {chain[i + 1]}…")

    if not jobs and requested == "auto" and "mock" not in chain:
        emit_progress("联网源均无结果，降级 mock 示例职位（不写入联网缓存）")
        jobs = MockJobSource().fetch(query, count)
        used = "mock"

    if jobs:
        emit_progress(f"职位搜索完成：source={used}，{len(jobs)} 条")
        # mock 兜底不要写进 auto:tavily>bocha 缓存，否则下次「命中」仍是 StartupXYZ
        with _DB_LOCK:
            if used == "mock" and live_configured and requested == "auto":
                persist_jobs(db, jobs)
            else:
                _save_cached_jobs(db, key, jobs)
    return jobs
