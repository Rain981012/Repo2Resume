"""职位匹配：画像 ↔ 职位双向打分。

【AI 辅助】模块：已确认接口 MatchScore = 向量相似度 + LLM 结构化评分。

性能策略（避免 10×串行 LLM 拖到数分钟）：
  1) 全量向量打分（本地 embed，秒级）
  2) 只对向量 top-N 做**一次**批量 LLM 打分
  3) LLM 超时/失败 → 退回纯向量排序，不把 score 打成 0
"""

from __future__ import annotations

import json
import logging
import re

import numpy as np

from repo2resume.llm.client import LLMClient
from repo2resume.retrieval.embedder import Embedder
from repo2resume.storage.models import Job, MatchScore, SkillProfile

logger = logging.getLogger(__name__)

# 批量 LLM 只评这么多条；其余只靠向量
_DEFAULT_LLM_TOP_N = 5
_JD_SNIPPET_CHARS = 400
_LLM_BATCH_TIMEOUT_S = 45.0


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


def _llm_score_job(
    llm: LLMClient,
    job: Job,
    profile_text: str,
    *,
    model: str | None = None,
) -> tuple[float, str]:
    """单职位 LLM 打分（保留给 match()）；失败返回 (0.0, \"\")。"""
    skills = ", ".join(job.skills)
    jd = (job.jd_text or "")[:_JD_SNIPPET_CHARS]
    prompt = (
        "You evaluate how well a candidate's skill profile matches a job.\n"
        "Return ONLY a JSON object with two fields:\n"
        "  score: a float between 0.0 and 1.0 (1.0 = perfect match),\n"
        "  reason: a short Chinese sentence explaining the score.\n\n"
        f"Candidate profile:\n{profile_text}\n\n"
        f"Job title: {job.title}\n"
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
            timeout_s=_LLM_BATCH_TIMEOUT_S,
        )
        data = _extract_json_payload(result.content)
        if not isinstance(data, dict):
            return 0.0, ""
        score = max(0.0, min(1.0, float(data.get("score", 0.0))))
        reason = str(data.get("reason", ""))
        return score, reason
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM job scoring failed: %s", exc)
        return 0.0, ""


def _llm_score_jobs_batch(
    llm: LLMClient,
    jobs: list[Job],
    profile_text: str,
    *,
    model: str | None = None,
) -> dict[str, tuple[float, str]]:
    """一次 LLM 调用给多职位打分。失败返回空 dict。"""
    if not jobs:
        return {}

    job_blocks: list[str] = []
    for job in jobs:
        skills = ", ".join(job.skills) if job.skills else "-"
        jd = (job.jd_text or "")[:_JD_SNIPPET_CHARS]
        job_blocks.append(
            f"- id: {job.id}\n"
            f"  title: {job.title}\n"
            f"  company: {job.company or 'Unknown'}\n"
            f"  skills: {skills}\n"
            f"  jd: {jd}"
        )
    prompt = (
        "根据候选人画像，给下列每个职位打匹配分。\n"
        "只输出 JSON 数组，每项字段：job_id (string), score (0~1 float), "
        "reason (一句中文)。不要其它文字。\n\n"
        f"候选人画像:\n{profile_text}\n\n"
        "职位列表:\n" + "\n".join(job_blocks) + "\n\nJSON:"
    )
    try:
        result = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            timeout_s=_LLM_BATCH_TIMEOUT_S,
        )
        data = _extract_json_payload(result.content)
        if isinstance(data, dict) and "scores" in data:
            data = data["scores"]
        if not isinstance(data, list):
            logger.warning("LLM batch scoring: expected list, got %s", type(data))
            return {}
        out: dict[str, tuple[float, str]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("job_id") or item.get("id") or "")
            if not jid:
                continue
            score = max(0.0, min(1.0, float(item.get("score", 0.0))))
            reason = str(item.get("reason", ""))
            out[jid] = (score, reason)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM batch job scoring failed: %s", exc)
        return {}


def _blend(
    vector_score: float,
    llm_score: float | None,
    reason: str = "",
) -> tuple[float, float | None, str]:
    """有 LLM 分则融合；否则纯向量，避免超时把 overall 拉成一半。"""
    if llm_score is not None:
        return (
            (vector_score + llm_score) / 2.0,
            llm_score,
            reason or "LLM 已评分",
        )
    return vector_score, None, reason or "按向量相似度排序（LLM 未得分）"


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

        llm_raw, reason = _llm_score_job(self.llm, job, profile_text)
        llm_score: float | None = llm_raw if reason else None
        overall, llm_out, reason = _blend(vector_score, llm_score, reason)
        return MatchScore(
            job_id=job.id,
            overall_score=round(overall, 4),
            vector_score=round(vector_score, 4),
            llm_score=round(llm_out, 4) if llm_out is not None else None,
            reason=reason,
        )

    def match_all(
        self,
        profile: SkillProfile,
        jobs: list[Job],
        *,
        top_k: int = 5,
        llm_top_n: int = _DEFAULT_LLM_TOP_N,
        use_llm: bool = True,
    ) -> list[MatchScore]:
        """批量匹配：全量向量 → 对 top-N 一次批量 LLM → 融合排序。"""
        from repo2resume.agent.progress import emit_progress

        if not jobs:
            return []

        emit_progress(f"向量预排 {len(jobs)} 条职位…")
        profile_text = _build_profile_text(profile)
        job_texts = [_build_job_text(j) for j in jobs]
        vectors = self.embedder.encode([profile_text, *job_texts])
        profile_vec = vectors[0]
        vector_by_id: dict[str, float] = {}
        for job, vec in zip(jobs, vectors[1:], strict=True):
            vector_by_id[job.id] = _cosine_similarity(profile_vec, vec)

        ranked = sorted(jobs, key=lambda j: vector_by_id[j.id], reverse=True)
        n_llm = max(0, min(llm_top_n, len(ranked))) if use_llm else 0
        llm_jobs = ranked[:n_llm]
        llm_scores: dict[str, tuple[float, str]] = {}
        if llm_jobs:
            emit_progress(f"批量 LLM 打分 top-{n_llm}（一次调用）…")
            llm_scores = _llm_score_jobs_batch(self.llm, llm_jobs, profile_text)

        scores: list[MatchScore] = []
        for job in ranked:
            v = vector_by_id[job.id]
            if job.id in llm_scores:
                llm_s, reason = llm_scores[job.id]
                overall, llm_out, reason = _blend(v, llm_s, reason)
            else:
                overall, llm_out, reason = _blend(v, None, "")
            scores.append(
                MatchScore(
                    job_id=job.id,
                    overall_score=round(overall, 4),
                    vector_score=round(v, 4),
                    llm_score=round(llm_out, 4) if llm_out is not None else None,
                    reason=reason,
                )
            )

        scores.sort(key=lambda s: s.overall_score, reverse=True)
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
