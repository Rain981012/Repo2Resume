"""招聘站点打标：官网高于招聘网站，招聘网站之间保持中性。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from repo2resume.jobs.bigtech import is_official_career_url, official_label

# 域名后缀 → (分数, 标签)。官网 1.0，垂类校招站略高，综合招聘站中性。
_BOARD_TIERS: tuple[tuple[str, float, str], ...] = (
    ("zhipin.com", 0.5, "Boss直聘"),
    ("liepin.com", 0.5, "猎聘"),
    ("zhaopin.com", 0.5, "智联"),
    ("lagou.com", 0.5, "拉勾"),
    ("51job.com", 0.5, "前程无忧"),
    ("nowcoder.com", 0.62, "牛客"),
    ("shixiseng.com", 0.62, "实习僧"),
    ("greenhouse.io", 0.5, "Greenhouse"),
    ("lever.co", 0.5, "Lever"),
    ("ashbyhq.com", 0.5, "Ashby"),
    ("app.mokahr.com", 0.78, "校招官网"),
)

OFFICIAL_SITE_SCORE = 1.0
SITE_BLEND_WEIGHT = 0.12

_DEFAULT_SCORE = 0.5
_DEFAULT_LABEL = "其它"
_SHELL_TITLES = {
    "岗位详情",
    "职位详情",
    "招聘官网",
    "校园招聘",
    "社会招聘",
    "首页",
    "岗位详情页",
    "职位详情页",
}
_SHELL_SUFFIX_RE = re.compile(r"(校园招聘官网|招聘官网|校园招聘|社会招聘)$")


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url.strip()).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    return host


def looks_like_job_title(title: str | None) -> bool:
    """标题是否像具体岗位，而不是「岗位详情|腾讯校招」这种壳。"""
    raw = (title or "").strip()
    if not raw:
        return False
    head = re.split(r"[|｜·•]", raw, maxsplit=1)[0].strip()
    compact = re.sub(r"[\s\-_/、，。]+", "", head)
    if not compact or compact in _SHELL_TITLES:
        return False
    if _SHELL_SUFFIX_RE.search(compact) and len(compact) <= 16:
        return False
    return True


def site_pref(url: str | None, title: str | None = None) -> tuple[float, str]:
    """返回 (site_score 0~1, 站点短名)。

    官网且标题像岗位才给 1.0；壳标题按招聘网站中性分，避免薄详情吃加成。
    title=None 时保持旧行为（测试/无标题调用）。
    """
    if is_official_career_url(url):
        label = official_label(url) or "大厂官网"
        if title is not None and not looks_like_job_title(title):
            return _DEFAULT_SCORE, label
        return OFFICIAL_SITE_SCORE, label
    host = _host(url)
    if not host:
        return _DEFAULT_SCORE, _DEFAULT_LABEL
    for suffix, score, label in _BOARD_TIERS:
        if host == suffix or host.endswith("." + suffix):
            return score, label
    return _DEFAULT_SCORE, _DEFAULT_LABEL


def site_score(url: str | None, title: str | None = None) -> float:
    return site_pref(url, title)[0]


def site_label(url: str | None, title: str | None = None) -> str:
    return site_pref(url, title)[1]


def is_boss_url(url: str | None) -> bool:
    host = _host(url)
    return host == "zhipin.com" or host.endswith(".zhipin.com")


def blend_with_site(
    match_score: float,
    url: str | None,
    *,
    title: str | None = None,
    weight: float = SITE_BLEND_WEIGHT,
) -> float:
    """final = (1-w)*匹配分 + w*站点分。官网相对招聘网站大约 +0.06。"""
    w = max(0.0, min(1.0, weight))
    return (1.0 - w) * match_score + w * site_score(url, title)


def sort_jobs_by_site(jobs: list, *, url_attr: str = "url") -> list:
    """无画像时：仅按站点偏好降序（稳定：同站保持原序）。"""
    indexed = list(enumerate(jobs))
    indexed.sort(key=lambda pair: (-site_score(getattr(pair[1], url_attr, None)), pair[0]))
    return [job for _, job in indexed]
