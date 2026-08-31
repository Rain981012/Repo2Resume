"""简历生成流水线：检索素材 → Writer-Critic → 落库 → 渲染。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient
from repo2resume.resume.critic import writer_critic_loop
from repo2resume.resume.render import render_project_experience, render_resume, write_markdown
from repo2resume.resume.writer import select_materials
from repo2resume.storage.db import Database
from repo2resume.storage.models import CritiqueReport, ResumeDraft, SearchHit, SkillProfile

logger = logging.getLogger(__name__)


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
        snap = db.load_latest_job_snapshot(job_id)
        if snap is not None and not text:
            text = (snap.content or "").strip()
        row = db.conn.execute(
            "SELECT id, title, jd_text, url, source FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"job id not found: {job_id}")
        title = row["title"]
        if not text:
            try:
                from repo2resume.jobs.detail import ensure_job_snapshot

                fetched = ensure_job_snapshot(
                    db, job_id=row["id"], url=row["url"], source=row["source"] or "detail_fetch"
                )
                if fetched is not None and (fetched.content or "").strip():
                    text = (fetched.content or "").strip()
            except Exception:
                pass
        if not text:
            text = (row["jd_text"] or "").strip() or row["title"]
        return text, row["id"], title
    if not text:
        raise ValueError("请提供 jd_text 或有效的 job_id")
    return text, job_id, title


def normalize_resume_reranker(raw: str | None) -> str:
    text = (raw or "off").strip().lower().replace("-", "_")
    if text in {"off", "none", "false", "0", "hybrid", ""}:
        return "off"
    if text in {"llm", "llm_rerank"}:
        return "llm"
    if text in {"cross_encoder", "crossencoder", "ce"}:
        return "cross_encoder"
    return "off"


def apply_resume_rerank(
    hits: list[SearchHit],
    query: str,
    *,
    mode: str,
    llm: LLMClient,
    top_k: int,
) -> tuple[list[SearchHit], str]:
    """对 hybrid 候选做精排。失败时回退原顺序。"""
    from repo2resume.agent.progress import emit_progress

    mode = normalize_resume_reranker(mode)
    if mode == "off" or not hits:
        return hits, "off"
    try:
        if mode == "llm":
            from repo2resume.retrieval.rerank import LLMReranker

            emit_progress("LLM 精排项目素材…")
            reranker = LLMReranker(llm)
            out = reranker.rerank(query, hits, top_k=top_k)
            return (out or hits[:top_k], "llm")
        from repo2resume.retrieval.rerank import CrossEncoderReranker

        emit_progress("cross-encoder 精排项目素材（首次加载模型可能较慢）…")
        reranker = CrossEncoderReranker()
        out = reranker.rerank(query, hits, top_k=top_k)
        return (out or hits[:top_k], "cross_encoder")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume rerank failed (%s); using hybrid order", exc)
        emit_progress(f"精排失败，回退 hybrid 顺序：{type(exc).__name__}")
        return hits, "hybrid_fallback"


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

    from repo2resume.analysis.fact_sheet import build_fact_sheet, fact_sheet_from_profile
    from repo2resume.storage.models import RepoStatsBundle

    stats = db.load_latest_repo_stats_bundle()
    if not isinstance(stats, RepoStatsBundle):
        stats = None

    emit_progress("索引画像与仓库统计素材…")
    store = VectorStore(db, embedder)
    n_chunks = store.upsert_profile_materials(profile, stats)
    if stats is None:
        emit_progress(
            "未找到已保存的 git 统计（仅索引画像摘要）。"
            "请先重新 analyze，以便写入 README/commits/依赖。"
        )
    else:
        emit_progress(f"已索引 {n_chunks} 条素材（含统计层 README/commits/依赖）…")

    emit_progress("混合检索相关项目…")
    candidate_k = max(top_k * 2, 8)
    hits = hybrid_search(store, resolved_jd, top_k=candidate_k)
    rerank_mode = "off"
    if hits:
        from repo2resume.observability.langsmith_span import span_call

        hits, rerank_mode = span_call(
            "resume.rerank",
            lambda current=hits: apply_resume_rerank(
                current,
                resolved_jd,
                mode=config.resume_reranker,
                llm=llm,
                top_k=candidate_k,
            ),
            inputs={"mode": config.resume_reranker, "n": len(hits)},
            outputs_of=lambda pair: {
                "mode": pair[1],
                "n": len(pair[0]),
                "top": [h.doc_id for h in pair[0][:5]],
            },
        )
        preserve = rerank_mode in {"llm", "cross_encoder"}
        label = {
            "llm": "LLM 精排",
            "cross_encoder": "cross-encoder 精排",
            "hybrid_fallback": "hybrid（精排失败回退）",
            "off": "hybrid",
        }.get(rerank_mode, rerank_mode)
        emit_progress(f"选用检索 top-{min(len(hits), top_k)}（{label}）…")
        materials = select_materials(
            jd_text=resolved_jd,
            hits=hits[:candidate_k],
            profile=profile,
            max_projects=4,
            preserve_hit_order=preserve,
        )
    else:
        materials = []

    # 有完整 stats 时用可核验事实清单；否则回退画像 caution 合成
    fact_sheet = build_fact_sheet(stats) if stats is not None else fact_sheet_from_profile(profile)

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
