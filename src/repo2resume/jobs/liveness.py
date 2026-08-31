"""推荐前核对详情页是否仍在招。

只看页面自身状态（已下线/404），不解析 JD 里的截止日期。
Boss 等站常 403/JS 壳页：核不出不算死亡，避免把主力源全部丢掉。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import httpx

from repo2resume.jobs.site_rank import is_boss_url
from repo2resume.storage.models import Job, JobVerification

logger = logging.getLogger(__name__)

_OFFLINE_MARKERS = (
    "职位已下线",
    "该职位已下线",
    "职位不存在",
    "岗位已关闭",
    "职位已关闭",
    "职位已失效",
    "岗位已失效",
    "职位已停止招聘",
    "该职位已停止招聘",
    "招聘已结束",
    "职位已结束",
    "已暂停招聘",
    "暂停招聘",
    "已招满",
    "职位已过期",
    "该职位已过期",
    "该职位不存在或已删除",
    "您访问的职位不存在",
    "job has expired",
    "no longer available",
    "position closed",
    "this job is closed",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_DETAIL_URL_RES = (
    re.compile(r"zhipin\.com/job_detail/[A-Za-z0-9_-]+", re.I),
    re.compile(r"liepin\.com/(?:job|a)/\d+", re.I),
    re.compile(r"jobs\.51job\.com/[^?\s]+/\d+\.html", re.I),
    re.compile(r"zhaopin\.com/jobdetail/[A-Za-z0-9]+\.htm", re.I),
    re.compile(r"jobs\.zhaopin\.com/[A-Za-z0-9]+\.htm", re.I),
    re.compile(r"lagou\.com/jobs/\d+", re.I),
    re.compile(r"talent\.alibaba\.com/.+", re.I),
    re.compile(r"job\.alibaba\.com/.+", re.I),
)

_YEAR_RE = re.compile(r"(20\d{2})")


def listing_status_from_html(html: str) -> str:
    """live / offline。只认负面文案，避免把 JS 壳页误判成下线。"""
    text = (html or "").lower()
    for marker in _OFFLINE_MARKERS:
        if marker.lower() in text:
            return "offline"
    return "live"


def _looks_like_detail_url(url: str) -> bool:
    u = (url or "").strip()
    if not u.startswith("http"):
        return False
    return any(rx.search(u) for rx in _DETAIL_URL_RES)


def _is_stale_campus_campaign(url: str, html: str) -> bool:
    lower_url = (url or "").lower()
    text = (html or "").lower()
    if "campus" not in lower_url and "校招" not in text and "校园招聘" not in text:
        return False
    years = [int(y) for y in _YEAR_RE.findall(f"{url}\n{html[:1200]}")]
    if not years:
        return False
    current = datetime.now(UTC).year
    return max(years) <= current - 2


def probe_listing(url: str, *, timeout_s: float = 8.0, client: httpx.Client | None = None) -> str:
    """兼容接口：返回 live / offline / blocked / unknown。"""
    return probe_listing_result(url, timeout_s=timeout_s, client=client).status


def probe_listing_result(
    url: str, *, timeout_s: float = 8.0, client: httpx.Client | None = None
) -> JobVerification:
    """结构化探测结果：status + method + checked_at。"""
    return probe_listing_detail(url, timeout_s=timeout_s, client=client)[0]


def probe_listing_detail(
    url: str, *, timeout_s: float = 8.0, client: httpx.Client | None = None
) -> tuple[JobVerification, str]:
    """探测状态，并把同一次响应的正文一并返回。

    判活本来就要下载整页，正文丢掉太可惜——公司名、年限、薪资、更新时间都在里面。
    只有判定为 live 时才回传正文，验证墙/下线页的内容没有解析价值。
    """
    checked_at = datetime.now(UTC).isoformat()

    def _v(status: str) -> JobVerification:
        return JobVerification(status=status, method="liveness_probe", checked_at=checked_at)

    if not url or not url.startswith("http"):
        return _v("unknown"), ""
    try:
        if client is not None:
            resp = client.get(url, headers=_HEADERS, follow_redirects=True, timeout=timeout_s)
        else:
            with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=_HEADERS) as http:
                resp = http.get(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("listing probe failed %s: %s", url, exc)
        return _v("unknown"), ""
    if resp.status_code in {404, 410, 451}:
        return _v("offline"), ""
    if resp.status_code >= 400:
        return _v("unknown"), ""
    final = str(getattr(resp, "url", "") or "").lower()
    html = resp.text or ""
    if any(p in final for p in ("/web/passport/", "security.html", "/captcha")):
        return _v("blocked"), ""
    head = html[:4000]
    if "security verification" in head.lower() or "人机验证" in head or "安全验证" in head[:800]:
        return _v("blocked"), ""
    if "请稍候" in head and ("zhipin" in final or "boss" in head.lower()):
        return _v("blocked"), ""
    # 详情链接被重定向到列表/搜索页，通常意味着该岗位已失效或下线
    if _looks_like_detail_url(url) and final and not _looks_like_detail_url(final):
        return _v("offline"), ""
    if _is_stale_campus_campaign(final or url, head):
        return _v("offline"), ""
    status = listing_status_from_html(html)
    return _v(status), (html if status == "live" else "")


def keep_open_jobs(
    jobs: list[Job],
    *,
    workers: int = 8,
    timeout_s: float = 8.0,
    client: httpx.Client | None = None,
    on_drop=None,
    on_detail=None,
) -> tuple[list[Job], int]:
    """丢掉已确认下线的职位。Boss 不拉详情页，按搜索摘要评分。
    其它站验证墙仍丢。mock / 核不出的保留。返回 (保留列表, 丢弃数)。
    on_drop(job, status) 的 status 为 offline / blocked。
    on_detail(job, html) 拿到判活时的页面正文，供调用方顺手补全字段。
    """
    if not jobs:
        return [], 0

    def _one(job: Job) -> tuple[Job, str, str]:
        if (job.source or "") == "mock" or not job.url:
            return job, "live", ""
        if is_boss_url(job.url):
            return job, "live", ""
        verification, html = probe_listing_detail(job.url, timeout_s=timeout_s, client=client)
        return job, verification.status, html

    kept: list[Job] = []
    dropped = 0
    n_workers = min(workers, len(jobs)) or 1
    status: dict[str, str] = {}
    html_by_id: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(_one, job) for job in jobs]
        for fut in as_completed(futs):
            job, st, html = fut.result()
            status[job.id] = st
            if html:
                html_by_id[job.id] = html
    for job in jobs:
        st = status.get(job.id, "unknown")
        drop = st == "offline" or (st == "blocked" and not is_boss_url(job.url))
        if drop:
            dropped += 1
            if on_drop is not None:
                on_drop(job, st)
            continue
        if on_detail is not None and html_by_id.get(job.id):
            try:
                on_detail(job, html_by_id[job.id])
            except Exception as exc:  # noqa: BLE001
                logger.info("detail enrich failed %s: %s", job.id, exc)
        kept.append(job)
    return kept, dropped
