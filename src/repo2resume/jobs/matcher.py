"""职位匹配：画像 ↔ 职位双向打分。

【AI 辅助】模块：已确认接口 MatchScore = 向量相似度 + LLM 结构化评分。

性能策略（避免 10×串行 LLM 拖到数分钟）：
  1) 全量向量 + 技能词重叠做粗排（本地，秒级）
  2) 只对粗排 top-N 做**一次**批量 LLM 打分
  3) LLM 超时/失败 → 退回粗排分数，不把 score 打成 0
  4) overall = 技能×0.40 + 城市×0.15 + 薪资×0.15 + 校招社招×0.15 + 摘要×0.15
     （未选校招/社招时该维为 1，不扣分；像样标题的大厂官网未写薪资不扣）
  5) 再与站点分混合：官网高于招聘网站
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import numpy as np

from repo2resume.jobs.bigtech import is_official_career_url, official_recruit_channel
from repo2resume.jobs.relevance import job_city_flag, split_pref_cities
from repo2resume.jobs.salary import extract_salary_info, salary_mid_k
from repo2resume.jobs.site_rank import (
    blend_with_site,
    is_boss_url,
    looks_like_job_title,
    site_pref,
)
from repo2resume.llm.client import LLMClient
from repo2resume.retrieval.embedder import Embedder
from repo2resume.storage.models import Job, MatchScore, SkillProfile

logger = logging.getLogger(__name__)

# 批量 LLM 只评这么多条；其余只靠粗排
_DEFAULT_LLM_TOP_N = 10
_JD_SNIPPET_CHARS = 1500
_SNIPPET_SCORE_NOTE = (
    "职位材料可能只是搜索摘要，不是完整 JD。"
    "缺城市/薪资/校招社招/职责就写「摘要未写明」，不要编成已匹配；"
    "技能分只根据已给出的 title 与文本，缺项的综合扣分由系统另算。"
)
_LLM_BATCH_TIMEOUT_FALLBACK_S = 60.0
# 粗排：匹配分 = (1-w)*向量 + w*技能重叠。重叠是为了捞回向量可能漏的专有名词岗。
_SKILL_OVERLAP_WEIGHT = 0.30
_W_SKILL = 0.40
_W_CITY = 0.15
_W_SALARY = 0.15
_W_CAMPUS = 0.15
_W_SNIPPET = 0.15
_LATIN_TOKEN = re.compile(r"[a-z][a-z0-9+#.]{1,}")
_CJK_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}")
_PARTTIME_MARKERS = ("兼职", "part-time", "part time")
_INTERN_MARKERS = ("实习", "intern", "校招")
_CAMPUS_TRUE_MARKERS = ("校招", "应届", "2026届", "2027届", "27届", "26届", "毕业生")
_CAMPUS_FALSE_MARKERS = ("社招", "社会招聘")
_SENIOR_YEARS_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[-~～到至]\s*(\d{1,2})\s*年"
)
_SENIOR_YEARS_SINGLE_RE = re.compile(
    r"(?<!\d)([3-9]|[1-9]\d)\s*年(?:以上|经验)"
)
_CAMPUS_SENIOR_TITLE = ("资深", "专家", "架构师", "staff", "principal", "fellow")
_DEV_DIRECTION_MARKERS = ("后端", "全栈", "开发", "backend", "fullstack", "frontend", "前端")
_RESEARCH_TITLE_MARKERS = ("研究", "科学家", "阿里星", "博士", "scientist", "research", "phd")
_DEV_TITLE_MARKERS = ("开发", "后端", "工程", "全栈", "前端", "研发", "工程师")
_CAMPUS_SENIOR_SCORE = 0.25
_DIRECTION_MISMATCH_FACTOR = 0.70


def _llm_timeout_s(llm: LLMClient) -> float:
    """跟全局 llm_timeout_s 走；弱模型可在 config 里调到 60–90，不必改代码。"""
    cfg = getattr(llm, "_config", None)
    raw = getattr(cfg, "llm_timeout_s", None) if cfg is not None else None
    if raw is None:
        return _LLM_BATCH_TIMEOUT_FALLBACK_S
    return float(raw)


def _tokens(*parts: str) -> set[str]:
    """技能/标题切词：保留拉丁专有名词与 ≥2 字中文，整段去空白也算一枚。"""
    out: set[str] = set()
    for part in parts:
        if not part or not str(part).strip():
            continue
        raw = str(part).strip().lower()
        collapsed = re.sub(r"\s+", "", raw)
        if len(collapsed) >= 2:
            out.add(collapsed)
        out.update(_LATIN_TOKEN.findall(raw))
        out.update(_CJK_TOKEN.findall(raw))
    return out


def _profile_skill_tokens(profile: SkillProfile) -> set[str]:
    stack = profile.tech_stack
    bits = [
        *stack.languages,
        *stack.frameworks,
        *stack.databases,
        *stack.tools_and_infra,
        *stack.other,
        profile.primary_direction,
        *profile.secondary_directions,
    ]
    return _tokens(*bits)


def _job_skill_tokens(job: Job) -> set[str]:
    """职位侧分母：有结构化 skills 只用 skills；Boss 摘要岗用标题+content 全文。"""
    structured = _tokens(*(job.skills or []))
    if structured:
        return structured
    if is_boss_url(job.url):
        return _tokens(job.title or "", job.jd_text or "")
    latin_from_jd = _LATIN_TOKEN.findall((job.jd_text or "").lower())
    return _tokens(job.title or "", *latin_from_jd)


def _skill_overlap(profile_tokens: set[str], job_tokens: set[str]) -> float:
    """JD 技能被画像覆盖的比例；职位无词则 0。"""
    if not job_tokens:
        return 0.0
    return len(profile_tokens & job_tokens) / len(job_tokens)


def _coarse_score(vector_score: float, skill_score: float) -> float:
    w = _SKILL_OVERLAP_WEIGHT
    return (1.0 - w) * vector_score + w * skill_score


def _employment_type(job: Job) -> str:
    blob = f"{job.title}\n{job.jd_text or ''}".lower()
    if any(m in blob for m in _PARTTIME_MARKERS):
        return "parttime"
    if any(m in blob for m in _INTERN_MARKERS):
        return "intern"
    return "fulltime"


def _has_senior_years(text: str) -> bool:
    """3 年起步或 5 年封顶的年限要求，视为社招；避开 2023年 这类年份。"""
    blob = text or ""
    for match in _SENIOR_YEARS_RANGE_RE.finditer(blob):
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo >= 3 or hi >= 5:
            return True
    return _SENIOR_YEARS_SINGLE_RE.search(blob) is not None


def _campus_flag(job: Job) -> str:
    """校招/社招三值。官网优先用站点自己的频道（/campus vs /social）。"""
    channel = official_recruit_channel(job.url)
    if channel == "social":
        return "false"
    if channel == "campus":
        return "true"
    blob = f"{job.title}\n{job.jd_text or ''}"
    blob_l = blob.lower()
    senior = _has_senior_years(blob)
    if senior:
        return "false"
    if any(m in blob_l for m in _CAMPUS_TRUE_MARKERS):
        return "true"
    if any(m in blob_l for m in _CAMPUS_FALSE_MARKERS):
        return "false"
    return "unknown"


def _city_match(job: Job, city: str | None) -> str:
    return job_city_flag(
        location=job.location or "",
        title=job.title or "",
        jd_text=job.jd_text or "",
        pref_city=city,
    )


def _is_job_eligible(
    job: Job,
    *,
    city: str | None,
    is_campus: bool | None,
    enforce_campus: bool = True,
) -> bool:
    role = _employment_type(job)
    if role == "parttime":
        return False
    if not looks_like_job_title(job.title):
        return False
    if enforce_campus:
        campus = _campus_flag(job)
        if is_campus is True and campus == "false":
            return False
        if is_campus is False and campus == "true":
            return False
    city_flag = _city_match(job, city)
    return city_flag != "false"


def _expect_salary_bounds(salary_range: str | None) -> tuple[float | None, float | None]:
    raw = (salary_range or "").strip().lower()
    if not raw:
        return None, None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _city_pref_score(job: Job, city: str | None) -> float:
    """命中偏好城=1；完全没写城市要扣分；明确外地岗=0。"""
    if not split_pref_cities(city):
        return 1.0
    flag = _city_match(job, city)
    if flag == "true":
        return 1.0
    if flag == "false":
        return 0.0
    return 0.32


def _campus_senior_title(job: Job) -> bool:
    title = (job.title or "").lower()
    return any(tok in title for tok in _CAMPUS_SENIOR_TITLE)


def _campus_pref_score(job: Job, is_campus: bool | None) -> float:
    """选了校招/社招才计分：写明且一致=1，没写扣分；未选择则中性。

    校招偏好下标题带资深/专家只降权，不从池子里拿掉。
    """
    if is_campus is None:
        return 1.0
    flag = _campus_flag(job)
    want = "true" if is_campus else "false"
    if flag == want:
        base = 1.0
    elif flag != "unknown":
        base = 0.0
    else:
        base = 0.32
    if is_campus is True and _campus_senior_title(job):
        return min(base, _CAMPUS_SENIOR_SCORE)
    return base


def _salary_pref_score(job: Job, salary_range: str | None) -> float:
    """有期望时：对得上=高，没写薪资要扣；无期望时有数字略高于空白。"""
    blob = f"{job.title or ''} {job.jd_text or ''}"
    info = extract_salary_info(blob)
    lo, hi = _expect_salary_bounds(salary_range)
    if not info.known:
        # 像样标题的大厂官网常不公示薪资；壳标题和招聘网站仍按缺失扣分
        if is_official_career_url(job.url) and looks_like_job_title(job.title):
            return 1.0
        return 0.30 if lo is not None else 0.55
    mid = salary_mid_k(info)
    jlo = float(info.min) if info.min is not None else mid
    jhi = float(info.max) if info.max is not None else mid
    if mid is None or jlo is None or jhi is None:
        return 0.30 if lo is not None else 0.55
    if lo is None:
        return 0.90
    if jhi < lo * 0.85:
        return 0.05
    cap = hi if hi is not None else lo
    if jlo > cap * 1.3:
        return 0.75
    return 1.0


def _snippet_pref_score(job: Job) -> float:
    """摘要越完整越高；标题党/空摘要压到很低。"""
    jd = (job.jd_text or "").strip()
    n = len(jd)
    stored = getattr(job, "content_completeness", None)
    if isinstance(stored, int | float):
        base = max(0.0, min(1.0, float(stored)))
    elif n >= 400:
        base = 1.0
    elif n >= 160:
        base = 0.70
    elif n >= 80:
        base = 0.42
    elif n >= 30:
        base = 0.22
    else:
        base = 0.08
    title_compact = re.sub(r"\s+", "", job.title or "")
    jd_compact = re.sub(r"\s+", "", jd)
    if jd_compact and title_compact and jd_compact in title_compact:
        base = min(base, 0.12)
    return base


def _profile_wants_dev(profile: SkillProfile) -> bool:
    blob = f"{profile.primary_direction} {' '.join(profile.secondary_directions)}".lower()
    return any(m in blob for m in _DEV_DIRECTION_MARKERS)


def _direction_fit_factor(profile: SkillProfile, job: Job) -> float:
    """后端/全栈方向遇到纯科研岗标题，技能维打七折。"""
    if not _profile_wants_dev(profile):
        return 1.0
    title = (job.title or "").lower()
    if not any(m in title for m in _RESEARCH_TITLE_MARKERS):
        return 1.0
    if any(m in title for m in _DEV_TITLE_MARKERS):
        return 1.0
    return _DIRECTION_MISMATCH_FACTOR


def _freshness_factor(job: Job) -> float:
    """久未更新的岗位多半已招满，但页面还挂着，判活探测抓不出来。

    做成乘性衰减而不是第六个权重项：抓不到更新时间的岗位（多数站点）
    保持 1.0 不受影响，只惩罚明确显示陈旧的。
    """
    days = getattr(job, "days_since_update", None)
    if not isinstance(days, int):
        return 1.0
    if days <= 14:
        return 1.0
    if days <= 30:
        return 0.96
    if days <= 60:
        return 0.90
    if days <= 90:
        return 0.82
    return 0.72


def _compose_overall(
    skill: float,
    city: float,
    salary: float,
    campus: float,
    snippet: float,
    freshness: float = 1.0,
) -> float:
    base = (
        _W_SKILL * skill
        + _W_CITY * city
        + _W_SALARY * salary
        + _W_CAMPUS * campus
        + _W_SNIPPET * snippet
    )
    return base * freshness


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个 L2 归一化向量间的余弦相似度。"""
    vec_a = np.array(a, dtype=np.float32)
    vec_b = np.array(b, dtype=np.float32)
    denom = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if denom == 0:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)


def _build_profile_text(profile: SkillProfile) -> str:
    """把画像渲染成一段可嵌入的文本。"""
    lines = [
        f"Primary direction: {profile.primary_direction}",
        f"Secondary directions: {', '.join(profile.secondary_directions)}",
    ]
    languages = [f"{lang.name} ({lang.share:.0%})" for lang in profile.coding_language]
    if languages:
        lines.append(f"Languages: {', '.join(languages)}")
    domains = [d.name for d in profile.domains]
    if domains:
        lines.append(f"Domains: {', '.join(domains)}")
    stack = profile.tech_stack
    for field in ["languages", "frameworks", "databases", "tools_and_infra", "other"]:
        items = getattr(stack, field)
        if items:
            lines.append(f"{field}: {', '.join(items)}")
    highlights = [h.claim for h in profile.highlights_pool]
    if highlights:
        lines.append("Highlights: " + "; ".join(highlights[:10]))
    return "\n".join(lines)


def _build_job_text(job: Job) -> str:
    """把职位渲染成一段可嵌入的文本。"""
    parts = [job.title]
    if job.company:
        parts.append(job.company)
    if job.skills:
        parts.append("Skills: " + ", ".join(job.skills))
    if job.jd_text:
        parts.append(job.jd_text)
    return "\n".join(parts)


def _extract_json_payload(text: str) -> object:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
        if not match:
            raise
        return json.loads(match.group(1))


def _analysis_from_llm_item(
    item: dict[str, Any], *, fallback_reason: str = ""
) -> tuple[str, dict[str, str]]:
    """从 LLM JSON 抽出四段分析；旧版只有 reason 时也能用。"""
    match = str(item.get("match") or "").strip()
    preference = str(item.get("preference") or "").strip()
    salary = str(item.get("salary") or "").strip()
    gaps = str(item.get("gaps") or "").strip()
    reason = str(item.get("reason") or "").strip() or fallback_reason
    if not match:
        match = reason
    if not salary:
        salary = "JD 未写明"
    composed = reason or "；".join(x for x in (match, preference, salary, gaps) if x)
    return composed, {
        "match": match,
        "preference": preference,
        "salary": salary,
        "gaps": gaps,
    }


def _llm_score_job(
    llm: LLMClient,
    job: Job,
    profile_text: str,
    *,
    model: str | None = None,
    prefs_text: str = "",
) -> tuple[float, str, dict[str, str]]:
    """单职位 LLM 打分（保留给 match()）；失败返回 (0.0, \"\", {})。"""
    skills = ", ".join(job.skills)
    jd = (job.jd_text or "")[:_JD_SNIPPET_CHARS]
    prompt = (
        "你评估候选人技能画像与该职位的匹配程度。\n"
        f"{_SNIPPET_SCORE_NOTE}\n"
        "只输出 JSON 对象，字段：\n"
        "  score: 0.0~1.0 的浮点数，\n"
        "  match: 匹配点（对得上的技能/方向，点名证据），\n"
        "  preference: 岗位的城市/职级/学历/校招要求与候选人偏好是否吻合；"
        "「未写明」只能用于 JD 一侧，\n"
        "  salary: JD 薪资 vs 候选人期望薪资；JD 未写明就说 JD 未写明并复述期望值，\n"
        "  gaps: 缺口与投递风险。\n\n"
        f"候选人画像:\n{profile_text}\n\n"
        + (f"{prefs_text}\n\n" if prefs_text else "")
        + f"Job title: {job.title}\n"
        f"Company: {job.company or 'Unknown'}\n"
        f"Required skills: {skills}\n"
        f"Job description:\n{jd}\n\n"
        "JSON:"
    )
    try:
        result = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            timeout_s=_llm_timeout_s(llm),
            role="score",
        )
        data = _extract_json_payload(result.content)
        if not isinstance(data, dict):
            return 0.0, "", {}
        score = max(0.0, min(1.0, float(data.get("score", 0.0))))
        reason, analysis = _analysis_from_llm_item(data)
        return score, reason, analysis
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM job scoring failed: %s", exc)
        return 0.0, "", {}


def _prefs_block(
    city: str | None,
    salary_range: str | None,
    is_campus: bool | None,
) -> str:
    """把用户偏好摊给 LLM。不给它就只能写「候选人期望未写明」。"""
    city_txt = (city or "").strip() or "未指定"
    salary_txt = (salary_range or "").strip() or "未指定"
    if is_campus is True:
        campus_txt = "校招/应届"
    elif is_campus is False:
        campus_txt = "社招"
    else:
        campus_txt = "未指定"
    return (
        "候选人求职偏好（这些是已知信息，不要写成「未写明」）:\n"
        f"  目标城市: {city_txt}\n"
        f"  期望薪资: {salary_txt}\n"
        f"  校招/社招: {campus_txt}"
    )


def _alias_maps(jobs: list[Job]) -> tuple[dict[str, str], dict[str, str]]:
    """J1..Jn → 真实 job.id。避免模型抄错 tavily-哈希。"""
    alias_to_id = {f"J{i}": job.id for i, job in enumerate(jobs, start=1)}
    id_to_alias = {job.id: f"J{i}" for i, job in enumerate(jobs, start=1)}
    return alias_to_id, id_to_alias


def _resolve_scored_job_id(raw: str, jobs: list[Job], alias_to_id: dict[str, str]) -> str:
    token = (raw or "").strip()
    if not token:
        return ""
    known = {j.id: j.id for j in jobs}
    if token in known:
        return token
    if re.fullmatch(r"J\d+", token, flags=re.I):
        return alias_to_id.get(token.upper(), "")
    return ""


def _parse_batch_scores(
    data: object, jobs: list[Job], alias_to_id: dict[str, str]
) -> dict[str, tuple[float, str, dict[str, str]]]:
    if isinstance(data, dict) and "scores" in data:
        data = data["scores"]
    if not isinstance(data, list):
        logger.warning("LLM batch scoring: expected list, got %s", type(data))
        return {}
    out: dict[str, tuple[float, str, dict[str, str]]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("job_id") or item.get("id") or "")
        jid = _resolve_scored_job_id(raw, jobs, alias_to_id) or raw.strip()
        if not jid:
            continue
        score = max(0.0, min(1.0, float(item.get("score", 0.0))))
        reason, analysis = _analysis_from_llm_item(item)
        out[jid] = (score, reason, analysis)
    return out


def _llm_score_jobs_batch(
    llm: LLMClient,
    jobs: list[Job],
    profile_text: str,
    *,
    model: str | None = None,
    prefs_text: str = "",
) -> dict[str, tuple[float, str, dict[str, str]]]:
    """一次 LLM 调用给多职位打分。失败返回空 dict。prompt 里用 J1..Jn，解析时映回真实 id。"""
    if not jobs:
        return {}

    alias_to_id, id_to_alias = _alias_maps(jobs)
    job_blocks: list[str] = []
    for job in jobs:
        alias = id_to_alias[job.id]
        skills = ", ".join(job.skills) if job.skills else "-"
        jd = (job.jd_text or "")[:_JD_SNIPPET_CHARS]
        job_blocks.append(
            f"- id: {alias}\n"
            f"  title: {job.title}\n"
            f"  company: {job.company or 'Unknown'}\n"
            f"  skills: {skills}\n"
            f"  jd: {jd}"
        )
    id_list = "、".join(id_to_alias[j.id] for j in jobs)
    prompt = (
        "根据候选人画像，给下列每个职位打匹配分。\n"
        f"{_SNIPPET_SCORE_NOTE}\n"
        f"必须为 {id_list} 各输出一项，一项都不能少。\n"
        "只输出 JSON 数组，每项字段：job_id（必须是 J1/J2 这种短编号）, "
        "score (0~1 float), match, preference, salary, gaps。\n"
        "match=对得上的技能/方向；"
        "preference=岗位的城市/职级/学历/校招要求与候选人偏好是否吻合，"
        "「未写明」只能用于 JD 一侧，候选人偏好一侧已给出，不得称其未写明；"
        "salary=JD 薪资 vs 候选人期望薪资（JD 未写明就说 JD 未写明，并复述期望值）；"
        "gaps=缺口与投递风险。"
        "不要其它文字。\n\n"
        f"候选人画像:\n{profile_text}\n\n"
        + (f"{prefs_text}\n\n" if prefs_text else "")
        + "职位列表:\n"
        + "\n".join(job_blocks)
        + "\n\nJSON:"
    )
    try:
        result = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            timeout_s=_llm_timeout_s(llm),
            role="score",
        )
        data = _extract_json_payload(result.content)
        return _parse_batch_scores(data, jobs, alias_to_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "api_key" in msg.lower() or "token" in msg.lower():
            msg = f"{type(exc).__name__}: completion failed"
        logger.warning("LLM batch job scoring failed: %s", msg[:240])
        return {}


def _heuristic_analysis(
    profile: SkillProfile,
    job: Job,
    *,
    skill_score: float,
    vector_score: float,
) -> dict[str, str]:
    """LLM 没给四段分析时，用标题、重合技能、JD 摘要拼。"""
    ptoks = _profile_skill_tokens(profile)
    jtoks = _job_skill_tokens(job)
    overlap = sorted(ptoks & jtoks)
    extra = sorted(jtoks - ptoks)
    noise = {
        "engineer",
        "developer",
        "backend",
        "frontend",
        "python",
        "招聘",
        "职位",
        "工程师",
        "开发",
        "岗位",
    }
    mismatch = [t for t in extra if t.lower() not in noise and len(t) >= 3][:6]
    title = (job.title or "该职位").strip()
    direction = (profile.primary_direction or "").strip() or "当前方向"
    if overlap:
        match = (
            f"岗位「{title}」对照主方向「{direction}」，能对上："
            + "、".join(overlap[:6])
            + "。"
        )
    else:
        match = f"岗位「{title}」对照主方向「{direction}」，摘要里几乎抽不到重合技能词。"
    loc = (job.location or "").strip()
    preference = f"工作地：{loc}。" if loc else "摘要未写清城市/职级。"
    salary = "摘要未写明"
    blob = f"{job.title or ''}\n{job.jd_text or ''}"
    m = re.search(
        r"(\d+(?:\.\d+)?\s*[-~～到至]\s*\d+(?:\.\d+)?\s*(?:k|K|千|万)(?:[·・]\d+薪)?)",
        blob,
    )
    if m:
        salary = m.group(1).replace(" ", "")
    if mismatch:
        gaps = "摘要还提到 " + "、".join(mismatch) + "，画像中缺少对应证据。"
    elif vector_score >= 0.7 and skill_score < 0.35:
        gaps = "向量分偏高，多半是泛词带动，业务不一定对口。"
    elif skill_score >= 0.5:
        gaps = "技能覆盖尚可，建议打开链接核对职级与业务后再投。"
    else:
        gaps = "整体相关度一般，更适合当备选。"
    if is_boss_url(job.url):
        gaps += "评分依据搜索摘要，未打开详情页。"
    return {
        "match": match,
        "preference": preference,
        "salary": salary,
        "gaps": gaps,
    }


def _heuristic_reason(
    profile: SkillProfile,
    job: Job,
    *,
    skill_score: float,
    vector_score: float,
) -> str:
    """LLM 没给理由时，用四段分析拼一段可读说明。"""
    a = _heuristic_analysis(
        profile, job, skill_score=skill_score, vector_score=vector_score
    )
    return "".join(a[k] for k in ("match", "preference", "salary", "gaps"))


def _blend(
    vector_score: float,
    llm_score: float | None,
    reason: str = "",
) -> tuple[float, float | None, str]:
    """有 LLM 分则与粗排分融合；否则用粗排，避免超时把 overall 拉成一半。"""
    if llm_score is not None:
        return (
            (vector_score + llm_score) / 2.0,
            llm_score,
            reason or "LLM 已评分",
        )
    return vector_score, None, reason or "按粗排分数排序（LLM 未得分）"


class JobMatcher:
    """把职位列表与画像做匹配打分。"""

    def __init__(self, embedder: Embedder, llm: LLMClient) -> None:
        self.embedder = embedder
        self.llm = llm

    def match(self, profile: SkillProfile, job: Job) -> MatchScore:
        """单职位匹配。"""
        profile_text = _build_profile_text(profile)
        job_text = _build_job_text(job)
        vectors = self.embedder.encode([profile_text, job_text])
        vector_score = _cosine_similarity(vectors[0], vectors[1])
        skill_score = _skill_overlap(_profile_skill_tokens(profile), _job_skill_tokens(job))
        match_score = _coarse_score(vector_score, skill_score)

        llm_raw, reason, analysis = _llm_score_job(self.llm, job, profile_text)
        llm_score: float | None = llm_raw if reason else None
        overall, llm_out, reason = _blend(match_score, llm_score, reason)
        overall = overall * _direction_fit_factor(profile, job)
        overall = _compose_overall(
            overall,
            _city_pref_score(job, None),
            _salary_pref_score(job, None),
            _campus_pref_score(job, None),
            _snippet_pref_score(job),
            _freshness_factor(job),
        )
        overall = blend_with_site(overall, job.url, title=job.title)
        need_heuristic = (
            llm_out is None
            or not reason.strip()
            or reason in {"LLM 已评分", "按粗排分数排序（LLM 未得分）"}
        )
        if need_heuristic:
            analysis = _heuristic_analysis(
                profile, job, skill_score=skill_score, vector_score=vector_score
            )
            reason = "".join(analysis[k] for k in ("match", "preference", "salary", "gaps"))
        elif not any(analysis.get(k) for k in ("match", "preference", "gaps")):
            analysis = _heuristic_analysis(
                profile, job, skill_score=skill_score, vector_score=vector_score
            )
        s_score, s_label = site_pref(job.url, job.title)
        return MatchScore(
            job_id=job.id,
            overall_score=round(overall, 4),
            vector_score=round(vector_score, 4),
            llm_score=round(llm_out, 4) if llm_out is not None else None,
            site_score=round(s_score, 4),
            site_label=s_label,
            reason=reason,
            analysis_match=analysis.get("match", ""),
            analysis_preference=analysis.get("preference", ""),
            analysis_salary=analysis.get("salary", ""),
            analysis_gaps=analysis.get("gaps", ""),
        )

    def match_all(
        self,
        profile: SkillProfile,
        jobs: list[Job],
        *,
        top_k: int = 5,
        llm_top_n: int = _DEFAULT_LLM_TOP_N,
        use_llm: bool = True,
        city: str | None = None,
        is_campus: bool | None = None,
        salary_range: str | None = None,
        enforce_campus: bool = True,
    ) -> list[MatchScore]:
        """批量匹配：向量+技能粗排 → 对 top-N 一次批量 LLM → 综合分排序。

        `enforce_campus=False` 只关掉校招硬门，校招偏好仍参与综合分。
        """
        from repo2resume.agent.progress import emit_progress

        if not jobs:
            return []

        eligible_jobs = [
            j
            for j in jobs
            if _is_job_eligible(
                j, city=city, is_campus=is_campus, enforce_campus=enforce_campus
            )
        ]
        if not eligible_jobs:
            return []

        emit_progress(f"硬约束过滤后 {len(eligible_jobs)}/{len(jobs)} 条进入打分…")
        emit_progress(f"向量+技能粗排 {len(eligible_jobs)} 条职位…")
        profile_text = _build_profile_text(profile)
        profile_tokens = _profile_skill_tokens(profile)
        job_texts = [_build_job_text(j) for j in eligible_jobs]
        vectors = self.embedder.encode([profile_text, *job_texts])
        profile_vec = vectors[0]
        vector_by_id: dict[str, float] = {}
        coarse_by_id: dict[str, float] = {}
        for job, vec in zip(eligible_jobs, vectors[1:], strict=True):
            v = _cosine_similarity(profile_vec, vec)
            sk = _skill_overlap(profile_tokens, _job_skill_tokens(job))
            vector_by_id[job.id] = v
            coarse_by_id[job.id] = _coarse_score(v, sk)

        ranked = sorted(eligible_jobs, key=lambda j: coarse_by_id[j.id], reverse=True)
        n_llm = max(0, min(llm_top_n, len(ranked))) if use_llm else 0
        llm_jobs = ranked[:n_llm]
        llm_scores: dict[str, tuple[float, str, dict[str, str]]] = {}
        prefs_text = _prefs_block(city, salary_range, is_campus)
        if llm_jobs:
            emit_progress(f"批量 LLM 打分 top-{n_llm}（一次调用，漏评会补打）…")
            llm_scores = _llm_score_jobs_batch(
                self.llm, llm_jobs, profile_text, prefs_text=prefs_text
            )
            missing = [j for j in llm_jobs if j.id not in llm_scores]
            if missing and llm_scores:
                emit_progress(f"补打 {len(missing)} 条漏评职位…")
                llm_scores.update(
                    _llm_score_jobs_batch(
                        self.llm, missing, profile_text, prefs_text=prefs_text
                    )
                )
            still_missing = [j.id for j in llm_jobs if j.id not in llm_scores]
            if still_missing:
                emit_progress(
                    f"仍有 {len(still_missing)} 条未拿到 LLM 分，改用技能/语义说明"
                )
            elif not llm_scores:
                emit_progress("LLM 打分超时/失败，改用技能与语义说明…")

        scores: list[MatchScore] = []
        for job in ranked:
            match_score = coarse_by_id[job.id]
            analysis: dict[str, str] = {}
            if job.id in llm_scores:
                llm_s, reason, analysis = llm_scores[job.id]
                skill_fit, llm_out, reason = _blend(match_score, llm_s, reason)
            else:
                skill_fit, llm_out, reason = _blend(match_score, None, "")
            skill_fit = skill_fit * _direction_fit_factor(profile, job)
            overall = _compose_overall(
                skill_fit,
                _city_pref_score(job, city),
                _salary_pref_score(job, salary_range),
                _campus_pref_score(job, is_campus),
                _snippet_pref_score(job),
                _freshness_factor(job),
            )
            overall = blend_with_site(overall, job.url, title=job.title)
            sk = _skill_overlap(profile_tokens, _job_skill_tokens(job))
            if llm_out is None or not reason.strip() or reason in {
                "LLM 已评分",
                "按粗排分数排序（LLM 未得分）",
            }:
                analysis = _heuristic_analysis(
                    profile,
                    job,
                    skill_score=sk,
                    vector_score=vector_by_id[job.id],
                )
                reason = "".join(analysis[k] for k in ("match", "preference", "salary", "gaps"))
            elif not any(analysis.get(k) for k in ("match", "preference", "gaps")):
                analysis = _heuristic_analysis(
                    profile,
                    job,
                    skill_score=sk,
                    vector_score=vector_by_id[job.id],
                )
            s_score, s_label = site_pref(job.url, job.title)
            salary = extract_salary_info(f"{job.title or ''} {job.jd_text or ''}")
            scores.append(
                MatchScore(
                    job_id=job.id,
                    overall_score=round(overall, 4),
                    vector_score=round(vector_by_id[job.id], 4),
                    llm_score=round(llm_out, 4) if llm_out is not None else None,
                    site_score=round(s_score, 4),
                    site_label=s_label,
                    reason=reason,
                    analysis_match=analysis.get("match", ""),
                    analysis_preference=analysis.get("preference", ""),
                    analysis_salary=analysis.get("salary", ""),
                    analysis_gaps=analysis.get("gaps", ""),
                    salary_known=salary.known,
                    eligibility_passed=True,
                )
            )

        scores.sort(
            key=lambda s: (s.overall_score, s.site_score or 0.0),
            reverse=True,
        )
        return scores[:top_k]


def score_jobs(
    profile: SkillProfile,
    jobs: list[Job],
    *,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 5,
) -> list[MatchScore]:
    """便捷函数：直接对职位列表打分。"""
    matcher = JobMatcher(embedder, llm)
    return matcher.match_all(profile, jobs, top_k=top_k)
