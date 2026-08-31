"""20 家大厂校招官网：检索名单、域名识别、公司别名。

挑法：用户给的秋招名单里，优先软件/互联网、在北上广深有技术岗、
官网能直接打开的。游戏厂只留米哈游；手机厂留小米；智造留大疆和海康。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class BigTechFirm:
    name: str
    aliases: tuple[str, ...]
    domains: tuple[str, ...]
    career_url: str
    label: str
    # Tavily 限域 site: 实测能召回职位详情页的才进官网检索名单
    tavily_indexed: bool = False


# 顺序：Tavily 能搜到岗的排前面，作为官网 site: 检索名单。
BIG_TECH: tuple[BigTechFirm, ...] = (
    BigTechFirm(
        "字节跳动",
        ("字节跳动", "字节", "抖音", "ByteDance"),
        ("jobs.bytedance.com",),
        "https://jobs.bytedance.com/campus",
        "字节校招",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "腾讯",
        ("腾讯", "Tencent"),
        ("join.qq.com", "careers.tencent.com"),
        "https://join.qq.com",
        "腾讯校招",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "美团",
        ("美团", "Meituan"),
        ("zhaopin.meituan.com", "campus.meituan.com"),
        "https://zhaopin.meituan.com",
        "美团招聘",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "哔哩哔哩",
        ("哔哩哔哩", "B站", "bilibili"),
        ("jobs.bilibili.com",),
        "https://jobs.bilibili.com/campus",
        "B站招聘",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "海康威视",
        ("海康威视", "海康", "Hikvision"),
        ("campushr.hikvision.com",),
        "https://campushr.hikvision.com",
        "海康校招",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "阿里巴巴",
        ("阿里巴巴", "阿里", "淘天", "阿里云", "Alibaba"),
        ("campus-talent.alibaba.com", "talent.alibaba.com", "job.alibaba.com"),
        "https://campus-talent.alibaba.com",
        "阿里校招",
        tavily_indexed=True,
    ),
    BigTechFirm(
        "滴滴",
        ("滴滴", "DiDi"),
        ("talent.didiglobal.com", "campus.didiglobal.com"),
        "https://talent.didiglobal.com/campus",
        "滴滴招聘",
    ),
    BigTechFirm(
        "蚂蚁集团",
        ("蚂蚁集团", "蚂蚁", "Ant Group"),
        ("talent.antgroup.com",),
        "https://talent.antgroup.com/campus",
        "蚂蚁招聘",
    ),
    BigTechFirm(
        "百度",
        ("百度", "Baidu"),
        ("talent.baidu.com", "campus.baidu.com"),
        "https://campus.baidu.com",
        "百度招聘",
    ),
    BigTechFirm(
        "京东",
        ("京东", "JD"),
        ("campus.jd.com", "zhaopin.jd.com", "talent.jd.com"),
        "https://campus.jd.com",
        "京东校招",
    ),
    BigTechFirm(
        "快手",
        ("快手", "Kuaishou"),
        ("campus.kuaishou.cn", "zhaopin.kuaishou.cn"),
        "https://campus.kuaishou.cn",
        "快手校招",
    ),
    BigTechFirm(
        "拼多多",
        ("拼多多", "PDD", "Temu"),
        ("careers.pinduoduo.com", "careers.pddglobalhr.com"),
        "https://careers.pinduoduo.com",
        "拼多多招聘",
    ),
    BigTechFirm(
        "小米",
        ("小米", "Xiaomi"),
        ("campus.hr.xiaomi.com", "hire.xiaomi.com"),
        "https://campus.hr.xiaomi.com",
        "小米招聘",
    ),
    BigTechFirm(
        "网易",
        ("网易", "NetEase"),
        ("campus.163.com", "hr.163.com"),
        "https://campus.163.com",
        "网易招聘",
    ),
    BigTechFirm(
        "大疆",
        ("大疆", "DJI"),
        ("we.dji.com",),
        "https://we.dji.com/zh-CN/campus",
        "大疆招聘",
    ),
    BigTechFirm(
        "蔚来",
        ("蔚来", "NIO"),
        ("campus.nio.com", "nio.jobs.feishu.cn"),
        "https://campus.nio.com",
        "蔚来招聘",
    ),
    BigTechFirm(
        "小鹏",
        ("小鹏", "XPeng"),
        ("xiaopeng.jobs.feishu.cn", "www.xiaopeng.com"),
        "https://xiaopeng.jobs.feishu.cn",
        "小鹏招聘",
    ),
    BigTechFirm(
        "米哈游",
        ("米哈游", "miHoYo"),
        ("jobs.mihoyo.com", "join.mihoyo.com"),
        "https://jobs.mihoyo.com",
        "米哈游校招",
    ),
    BigTechFirm(
        "得物",
        ("得物", "Poizon"),
        ("campus.dewu.com",),
        "https://campus.dewu.com",
        "得物校招",
    ),
    BigTechFirm(
        "MiniMax",
        ("MiniMax", "minimax", "稀宇"),
        ("www.minimax.cn", "minimax.cn"),
        "https://www.minimax.cn/careers",
        "MiniMax招聘",
    ),
)

DEFAULT_BIG_TECH: tuple[str, ...] = tuple(dict.fromkeys(f.name for f in BIG_TECH))


def tavily_searchable_firms() -> tuple[BigTechFirm, ...]:
    return tuple(f for f in BIG_TECH if f.tavily_indexed)


_CAMPUS_ONLY_HOSTS = {
    "join.qq.com",
    "campushr.hikvision.com",
    "campus.jd.com",
    "campus.meituan.com",
    "campus.163.com",
    "campus.baidu.com",
    "campus.kuaishou.cn",
    "campus.nio.com",
    "campus.dewu.com",
    "campus.didiglobal.com",
    "campus.hr.xiaomi.com",
    "campus-talent.alibaba.com",
    "careers.pinduoduo.com",
    "careers.pddglobalhr.com",
    "jobs.mihoyo.com",
}
# 整站就是社招（路径里的校招页例外，先看 path）
_SOCIAL_ONLY_HOSTS = {
    "careers.tencent.com",
    "hr.163.com",
    "zhaopin.kuaishou.cn",
    "zhaopin.jd.com",
    "hire.xiaomi.com",
    "nio.jobs.feishu.cn",
    "xiaopeng.jobs.feishu.cn",
    "talent.antgroup.com",
    "talent.alibaba.com",
}
_CAMPUS_PATH_MARKERS = (
    "/campus",
    "/school",
    "/campusrecruit",
    "/jobs/campus",
    "highlighttype=campus",
)
_SOCIAL_PATH_MARKERS = (
    "/social",
    "/experienced",
    "/society",
    "/jobs/social",
    "/off-campus",
    "highlighttype=social",
)
# 校招检索用带路径的 site:。Tavily 对滴滴 /campus 无效（仍返回 /social），故不搜滴滴官网。
_CAMPUS_SITE_QUERY = {
    "jobs.bytedance.com": "jobs.bytedance.com/campus",
    "jobs.bilibili.com": "jobs.bilibili.com/campus",
    # 美团同一域名用 query 区分频道
    "zhaopin.meituan.com": "zhaopin.meituan.com highlightType=campus",
}
_BROAD_HOSTS = {"xiaopeng.com", "minimax.cn"}
_BROAD_PATH_TOKENS = ("join", "career", "job", "campus", "recruit")
_OFFICIAL_HOME_PATHS = {
    "",
    "/",
    "/campus",
    "/campus/",
    "/jobs",
    "/jobs/",
    "/careers",
    "/careers/",
    "/join.html",
    "/zh-CN/campus",
    "/zh-CN/campus/",
    "/experienced",
    "/experienced/",
    "/social",
    "/social/",
    "/zh-CN/social",
    "/zh-CN/social/",
    "/off-campus",
    "/off-campus/",
}


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url.strip()).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _firm_for_host(host: str) -> BigTechFirm | None:
    if not host:
        return None
    for firm in BIG_TECH:
        for domain in firm.domains:
            d = domain.lower().removeprefix("www.")
            if host == d or host.endswith("." + d):
                return firm
    return None


def official_domains() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for firm in BIG_TECH:
        for domain in firm.domains:
            if domain not in seen:
                seen.add(domain)
                out.append(domain)
    return out


def is_official_career_url(url: str | None) -> bool:
    host = _host(url)
    if _firm_for_host(host) is None:
        return False
    if host in _BROAD_HOSTS:
        path = (urlparse((url or "").strip()).path or "").lower()
        return any(tok in path for tok in _BROAD_PATH_TOKENS)
    return True


def official_company_name(url: str | None) -> str | None:
    firm = _firm_for_host(_host(url))
    return firm.name if firm else None


def official_label(url: str | None) -> str | None:
    firm = _firm_for_host(_host(url))
    if firm is None:
        return None
    channel = official_recruit_channel(url)
    if channel == "social":
        return f"{firm.name}社招"
    if channel == "campus":
        return firm.label if "校招" in firm.label else f"{firm.name}校招"
    return firm.label


def official_recruit_channel(url: str | None) -> str | None:
    """官网自己的校招/社招分类。看路径和主机，不看 JD 摘要。

    返回 campus / social / None（官网但分不清频道）。
    """
    if not is_official_career_url(url):
        return None
    host = _host(url)
    parsed = urlparse((url or "").strip())
    blob = f"{parsed.path}?{parsed.query}".lower()
    if any(tok in blob for tok in _SOCIAL_PATH_MARKERS):
        return "social"
    if any(tok in blob for tok in _CAMPUS_PATH_MARKERS):
        return "campus"
    if host in _CAMPUS_ONLY_HOSTS:
        return "campus"
    if host in _SOCIAL_ONLY_HOSTS:
        return "social"
    return None


def is_official_job_url(url: str | None) -> bool:
    """官网岗位详情，排除招聘首页/校招活动页。"""
    if not is_official_career_url(url):
        return False
    try:
        path = urlparse((url or "").strip()).path.lower().rstrip("/")
    except Exception:  # noqa: BLE001
        return False
    home = path if path.startswith("/") else f"/{path}"
    if home in _OFFICIAL_HOME_PATHS or home + "/" in _OFFICIAL_HOME_PATHS:
        return False
    # xiaopeng.com 只有 join.html 是招聘入口，岗位页在飞书子域
    if _host(url) in {"xiaopeng.com"} and not path.startswith("/join"):
        return False
    return True


def is_big_tech_job(job: object) -> bool:
    """官网链接，或公司名/标题能对上 20 家里的别名。"""
    url = str(getattr(job, "url", "") or "")
    if is_official_career_url(url):
        return True
    blob = f"{getattr(job, 'company', '') or ''} {getattr(job, 'title', '') or ''}"
    for firm in BIG_TECH:
        if any(alias and alias in blob for alias in firm.aliases):
            return True
    return False


def official_site_queries(
    role: str,
    *,
    kind: str = "校招",
    offset: int = 0,
    limit: int | None = None,
    skip: set[str] | None = None,
) -> list[str]:
    """Tavily 实测能召回详情的官网；校招检索带上 /campus 或 highlightType。"""
    role_s = " ".join((role or "").split()) or "后端开发"
    kind_s = (kind or "").strip()
    seen = set(skip or ())
    out: list[str] = []
    indexed = tavily_searchable_firms()
    firms = indexed[offset:] if offset else indexed
    for firm in firms:
        host = firm.domains[0]
        scope = host
        if kind_s == "校招":
            scope = _CAMPUS_SITE_QUERY.get(host, host)
        q = " ".join(p for p in (role_s, kind_s, f"site:{scope}") if p)
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
        if limit is not None and len(out) >= limit:
            break
    return out
