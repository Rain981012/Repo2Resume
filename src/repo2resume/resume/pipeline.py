"""简历生成流水线：检索素材 → Writer-Critic → 落库 → 渲染。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.resume.critic import writer_critic_loop
from repo2resume.resume.render import render_project_experience, render_resume, write_markdown
from repo2resume.resume.writer import select_materials
from repo2resume.storage.db import Database
from repo2resume.storage.models import CritiqueReport, ResumeDraft, SkillProfile


@dataclass
class ResumePipelineResult:
    draft: ResumeDraft
    reports: list[CritiqueReport]
    markdown: str
    output_path: Path | None
    draft_id: int | None


def _resolve_jd_text(
    db: Database | None,
    *,
    jd_text: str = "",
    job_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    """返回 (jd_text, job_id, job_title)。"""
    text = (jd_text or "").strip()
    title: str | None = None
    if job_id and db is not None:
        row = db.conn.execute(
            "SELECT id, title, jd_text FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"job id not found: {job_id}")
        title = row["title"]
        if not text:
            text = (row["jd_text"] or "").strip() or row["title"]
        return text, row["id"], title
    if not text:
        raise ValueError("请提供 jd_text 或有效的 job_id")
    return text, job_id, title


def run_resume_pipeline(
    *,
    config: AppConfig,
    db: Database,
    embedder: object,
    llm: LLMClient,
    profile: SkillProfile,
    jd_text: str = "",
    job_id: str | None = None,
    output: Path | None = None,
    full_resume: bool = True,
    strip_src: bool = False,
    top_k: int = 8,
    max_rounds: int = 2,
) -> ResumePipelineResult:
    """端到端生成项目经历（含 Critic 循环），可选写文件并入库。"""
    from repo2resume.agent.progress import emit_progress
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.store import VectorStore

    emit_progress("解析目标职位 / JD…")
    resolved_jd, resolved_job_id, job_title = _resolve_jd_text(db, jd_text=jd_text, job_id=job_id)
    from repo2resume.resume.writer import sanitize_job_title

    job_title = sanitize_job_title(job_title)

    emit_progress("索引画像素材…")
    store = VectorStore(db, embedder)
    store.upsert_profile_materials(profile, None)
    emit_progress("混合检索相关项目…")
    hits = hybrid_search(store, resolved_jd, top_k=max(top_k * 2, 8))
    if hits:
        # MVP：跳过 LLM rerank（glm 上常要 1–2 分钟）；hybrid 顺序已够用。
        # Phase 5 可用 cross-encoder / 开关重新打开精排。
        emit_progress(f"选用检索 top-{min(len(hits), top_k)}（跳过 LLM 重排以加速）…")
        materials = select_materials(
            jd_text=resolved_jd,
            hits=hits[: max(top_k * 2, top_k)],
            profile=profile,
            max_projects=4,
        )
    else:
        materials = []

    from repo2resume.analysis.fact_sheet import fact_sheet_from_profile

    # 简历阶段通常没有完整 stats：用画像 caution 合成 author_share / low_author_share
    fact_sheet = fact_sheet_from_profile(profile)

    emit_progress("Writer-Critic 起草与审稿…")
    draft, reports = writer_critic_loop(
        jd_text=resolved_jd,
        materials=materials,
        profile=profile,
        fact_sheet=fact_sheet,
        locale="zh-CN",
        llm=llm,
        max_rounds=max_rounds,
        job_id=resolved_job_id,
        job_title=job_title,
    )

    emit_progress("保存草稿并渲染 Markdown…")
    draft_id = db.save_resume_draft(draft, job_id=resolved_job_id)

    if full_resume:
        markdown = render_resume(draft, profile=profile, config=config, strip_src=strip_src)
    else:
        markdown = render_project_experience(draft, strip_src=strip_src)

    out_path: Path | None = None
    if output is not None:
        out_path = write_markdown(output, markdown)
        emit_progress(f"已写入 {out_path}")

    return ResumePipelineResult(
        draft=draft,
        reports=reports,
        markdown=markdown,
        output_path=out_path,
        draft_id=draft_id,
    )
