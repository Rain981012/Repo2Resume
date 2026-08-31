"""从详情页正文解析结构化字段。

正文由 `liveness.probe_listing_detail` 判活时顺手带回，不额外发请求。
目前只有猎聘可解析：51job 返回 WAF 加密壳，智联/Boss 是验证墙。
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from repo2resume.jobs.detail import _extract_text

# 猎聘详情页标题形如：
# 【广州 Python+SQL+英语口语良好招聘】-华钦科技广州招聘信息-猎聘 … 12-22k 广州-天河区 5-10年 本科 …
_LIEPIN_COMPANY_RE = re.compile(r"-\s*([^-【】]{2,24}?)招聘信息\s*-\s*猎聘")
_SALARY_RE = re.compile(r"(\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?\s*[kK万](?:\s*·\s*\d+薪)?)")
_EXPERIENCE_RE = re.compile(
    r"(经验不限|应届|在校生|\d{1,2}\s*[-~]\s*\d{1,2}\s*年|\d{1,2}\s*年以上)"
)
_EDUCATION_RE = re.compile(r"(统招本科|学历不限|本科|硕士|博士|大专|中专|高中)")
_UPDATED_RE = re.compile(r"(今日更新|昨日更新|(\d+)\s*天前更新|(\d+)\s*小时前更新)")
_CAMPUS_RE = re.compile(r"(学生可投|应届|校园招聘|校招)")
# 页头是站点导航，正文一般从「职位描述 / 岗位职责」之类的小标题开始
_JD_START_RE = re.compile(
    r"(职位介绍|职位描述|岗位职责|职位职责|工作职责|岗位描述|职位要求|任职要求|岗位要求)"
)
# 部分岗位没有小标题，正文紧跟页头的分享按钮；用它作为兜底起点
_JD_FALLBACK_START_RE = re.compile(r"(微信分享扫码|收藏\s*微信分享)")
_JD_END_RE = re.compile(
    r"(其他信息|公司简介|公司信息|工商信息|猎聘温馨提示|猜你喜欢|相似职位|相关职位"
    r"|举报该职位|该公司其他职位|了解更多安全防范)"
)


class JobDetail(BaseModel):
    """详情页解析结果；解析不到的字段留 None，不臆造。"""

    company: str | None = None
    location: str | None = None
    salary_text: str | None = None
    experience: str | None = None
    education: str | None = None
    updated_text: str | None = None
    days_since_update: int | None = None
    is_campus: bool | None = None
    jd_text: str | None = None

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.company,
                self.location,
                self.salary_text,
                self.experience,
                self.education,
                self.updated_text,
                self.jd_text,
            )
        )


def _days_from_updated(token: str) -> int | None:
    if not token:
        return None
    if "今日" in token:
        return 0
    if "昨日" in token:
        return 1
    if "小时前" in token:
        return 0
    m = re.search(r"(\d+)\s*天前", token)
    return int(m.group(1)) if m else None


def _clean_company(raw: str) -> tuple[str | None, str | None]:
    """返回 (公司名, 工作地)。

    标题里的公司名带城市后缀（`华为深圳`）；`猎头顾问深圳` 这类是代招，
    真实雇主页面上本来就不公开，标成「猎头代招」比写公司名更诚实。
    """
    from repo2resume.jobs.liepin_mcp import _KNOWN_CITIES

    name = re.sub(r"(招聘信息|招聘)$", "", (raw or "").strip()).strip()
    city: str | None = None
    for known in _KNOWN_CITIES:
        if name.endswith(known) and len(name) > len(known):
            city = known
            name = name[: -len(known)].strip()
            break
    if not name:
        return None, city
    if name in {"猎头顾问", "猎头", "招聘顾问"}:
        return "猎头代招", city
    return name, city


def _slice_jd(text: str) -> str | None:
    hit = _JD_START_RE.search(text)
    if hit is not None:
        body = text[hit.start() :]
    else:
        fallback = _JD_FALLBACK_START_RE.search(text)
        if fallback is None:
            return None
        body = text[fallback.end() :]
    end = _JD_END_RE.search(body)
    if end is not None:
        body = body[: end.start()]
    body = body.strip()
    return body[:4000] if len(body) >= 60 else None


def parse_liepin_detail(html_or_text: str) -> JobDetail:
    """猎聘详情页：关键字段都在页头一行里，JD 正文在小标题之后。"""
    text = _extract_text(html_or_text) if "<" in (html_or_text or "")[:400] else html_or_text
    text = (text or "").strip()
    if not text:
        return JobDetail()

    head = text[:800]
    detail = JobDetail()

    m = _LIEPIN_COMPANY_RE.search(head)
    if m:
        detail.company, detail.location = _clean_company(m.group(1))

    m = _SALARY_RE.search(head)
    if m:
        detail.salary_text = re.sub(r"\s+", "", m.group(1))

    m = _EXPERIENCE_RE.search(head)
    if m:
        detail.experience = re.sub(r"\s+", "", m.group(1))

    m = _EDUCATION_RE.search(head)
    if m:
        detail.education = m.group(1)

    m = _UPDATED_RE.search(head)
    if m:
        detail.updated_text = re.sub(r"\s+", "", m.group(1))
        detail.days_since_update = _days_from_updated(detail.updated_text)

    if _CAMPUS_RE.search(head):
        detail.is_campus = True

    detail.jd_text = _slice_jd(text)
    return detail


def parse_detail(url: str, html: str) -> JobDetail:
    """按站点分派。不认识的站点返回空结果，调用方保持原字段。"""
    u = (url or "").lower()
    if "liepin.com" in u:
        return parse_liepin_detail(html)
    return JobDetail()
