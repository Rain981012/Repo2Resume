"""搜岗相关性门禁：官方源也不能免检。"""

from __future__ import annotations

import re
from typing import Any

_CITIES = (
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

_STOP = (
    "校招",
    "社招",
    "应届",
    "远程",
    "全国",
    "实习生",
    "工程师",
    "岗位",
    "职位",
    "招聘",
)

_HARDWARE_TITLE = (
    "fpga",
    "asic",
    "cad",
    "mcu",
    "芯片",
    "半导体",
    "soc",
    "rdma",
    "数字设计",
    "硬件加速",
)

_SOFTWARE_QUERY = (
    "python",
    "后端",
    "backend",
    "前端",
    "frontend",
    "开发",
    "fastapi",
    "django",
    "ai应用",
    "人工智能",
)

# 区域口头禅：禁止原样入库。展开成具体城市是 LLM 在 set_job_prefs 的职责。
_REGION_ALIASES = {
    "北上广深",
    "北上广",
    "江浙沪",
    "京津冀",
    "珠三角",
    "长三角",
}


def split_pref_cities(city: str | None) -> list[str]:
    """按逗号等分隔符拆成城市名；不解释「北上广深」这类缩写。"""
    raw = (city or "").strip()
    if not raw or raw.lower() in {"null", "none", "不限", "全国"}:
        return []
    parts = [p.strip() for p in re.split(r"[/、，,和与\s]+", raw) if p.strip()]
    return parts or [raw]


def looks_like_region_alias(city: str | None) -> bool:
    """city 仍是区域缩写、尚未展开成具体城市名。"""
    parts = split_pref_cities(city)
    if not parts:
        text = (city or "").strip()
        return text in _REGION_ALIASES
    return any(p in _REGION_ALIASES for p in parts)


def job_city_flag(
    *,
    location: str = "",
    title: str = "",
    jd_text: str = "",
    pref_city: str | None,
) -> str:
    """true=命中偏好城；false=写了别的已知城；unknown=完全没城市。"""
    prefs = split_pref_cities(pref_city)
    if not prefs:
        return "unknown"
    blob = f"{location}\n{title}\n{jd_text}"
    if any(city in blob for city in prefs):
        return "true"
    pref_set = set(prefs)
    if any(city in blob for city in _CITIES if city not in pref_set):
        return "false"
    return "unknown"


def query_role_tokens(query: str) -> list[str]:
    """从搜索词里抽出岗位相关 token（去掉城市和校招等噪声）。"""
    text = " ".join((query or "").split())
    if not text:
        return []
    for city in _CITIES:
        text = text.replace(city, " ")
    for stop in _STOP:
        text = text.replace(stop, " ")
    out: list[str] = []
    seen: set[str] = set()
    for tok in text.split():
        key = tok.casefold()
        if len(tok) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def job_matches_query(job: Any, query: str) -> bool:
    """标题/JD 是否和本次查询相关；软件岗查询会丢掉芯片/FPGA 标题。"""
    title = str(getattr(job, "title", "") or "")
    jd = str(getattr(job, "jd_text", "") or "")
    company = str(getattr(job, "company", "") or "")
    blob = f"{title}\n{jd}\n{company}".casefold()
    title_l = title.casefold()
    q = (query or "").casefold()

    software_q = any(h in q for h in _SOFTWARE_QUERY)
    if software_q and any(h in title_l for h in _HARDWARE_TITLE):
        return False

    tokens = query_role_tokens(query)
    if not tokens:
        return True
    return any(tok.casefold() in blob for tok in tokens)
