"""职位匹配：画像 ↔ 职位双向打分。

【AI 辅助】模块：已确认接口 MatchScore = 向量相似度 + LLM 结构化评分。
"""

from __future__ import annotations

import json
import logging

import numpy as np

from repo2resume.llm.client import LLMClient
from repo2resume.retrieval.embedder import Embedder
from repo2resume.storage.models import Job, MatchScore, SkillProfile

logger = logging.getLogger(__name__)


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


def _llm_score_job(
    llm: LLMClient,
    job: Job,
    profile_text: str,
    *,
    model: str | None = None,
) -> tuple[float, str]:
    """用 LLM 结构化输出给职位匹配度打分。"""
    skills = ", ".join(job.skills)
    prompt = (
        "You evaluate how well a candidate's skill profile matches a job.\n"
        "Return ONLY a JSON object with two fields:\n"
        "  score: a float between 0.0 and 1.0 (1.0 = perfect match),\n"
        "  reason: a short Chinese sentence explaining the score.\n\n"
        f"Candidate profile:\n{profile_text}\n\n"
        f"Job title: {job.title}\n"
        f"Company: {job.company or 'Unknown'}\n"
        f"Required skills: {skills}\n"
        f"Job description:\n{job.jd_text}\n\n"
        "JSON:"
    )
    try:
        result = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
        )
        text = result.content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        score = float(data.get("score", 0.0))
        reason = str(data.get("reason", ""))
        score = max(0.0, min(1.0, score))
        return score, reason
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM job scoring failed: %s", exc)
        return 0.0, ""


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

        llm_score, reason = _llm_score_job(self.llm, job, profile_text)

        overall = (vector_score + llm_score) / 2.0
        return MatchScore(
            job_id=job.id,
            overall_score=round(overall, 4),
            vector_score=round(vector_score, 4),
            llm_score=round(llm_score, 4),
            reason=reason,
        )

    def match_all(
        self,
        profile: SkillProfile,
        jobs: list[Job],
        *,
        top_k: int = 5,
    ) -> list[MatchScore]:
        """批量匹配并排序。"""
        scores = [self.match(profile, job) for job in jobs]
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
