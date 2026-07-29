"""简历 Critic：程序化硬过滤 + LLM 审稿；与 Writer 组成 ≤2 轮循环。

【AI 辅助】已确认契约：
  - CritiqueReport；critique / revise_experience / writer_critic_loop
  - 默认只强制消化 must_fix
  - 禁词、空 evidence 在 LLM 前硬过滤
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from repo2resume.llm.client import LLMClient
from repo2resume.resume.writer import (
    _extract_json,
    _hit_repo,
    ensure_bullet_evidence,
    write_experience,
)
from repo2resume.storage.models import (
    CritiqueItem,
    CritiqueReport,
    FactSheet,
    ResumeDraft,
    SearchHit,
    SkillProfile,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 可见正文禁区（阶段 A 04 / 03）；命中 → must
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"author_share",
        r"贡献占比",
        r"commit\s*数",
        r"commit\s*次",
        r"\d+\s*个\s*commit",
        r"多人协作",
        r"本人部分贡献",
        r"贡献占比低",
        r"团队仓仅",
    )
)

# 易把团队仓写成个人整站主导的措辞 → must（收窄到模块级）
_OVERCLAIM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"独立完成整个",
        r"独立开发整[个套]",
        r"设计并实现完整",
        r"构建了完整的.{0,12}系统",
        r"开发了全部",
        r"负责整个系统",
        r"从零搭建整[个套]",
    )
)


def programmatic_checks(draft: ResumeDraft) -> list[CritiqueItem]:
    """确定性检查：空 evidence / 空正文 / 禁词。在 LLM critique 之前调用。"""
    items: list[CritiqueItem] = []
    for project in draft.projects:
        visible_heads = f"{project.project_name}\n{project.one_liner}"
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(visible_heads):
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="fact",
                        target=project.project_name,
                        message=(
                            f"项目名或一句话总结含禁止表述（匹配 {pat.pattern}），改为纯产品定位。"
                        ),
                    )
                )
                break

        if not project.bullets:
            items.append(
                CritiqueItem(
                    severity="must",
                    category="structure",
                    target=project.project_name,
                    message=("该项目无经历 bullet；相关较弱也至少保留有证据的条目，否则移出本稿。"),
                )
            )
            continue

        for idx, bullet in enumerate(project.bullets, start=1):
            target = f"{project.project_name} / 第{idx}条"
            if not (bullet.evidence.source or "").strip():
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="fact",
                        target=target,
                        message="evidence.source 为空；补仓库名与依据，或删除该条。",
                    )
                )
            if re.search(r"\s-\s+\*\*", bullet.body):
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="structure",
                        target=target,
                        message=(
                            "body 内嵌了其它 bullet（出现「 - **标签**：」）；"
                            "拆成独立 bullets 数组项，每条一个 label/body/evidence。"
                        ),
                    )
                )
            if not bullet.label.strip() or not bullet.body.strip():
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="style",
                        target=target,
                        message="短标签或正文为空；补全 **短标签** + 密度 STAR，或删除。",
                    )
                )
            blob = f"{bullet.label}\n{bullet.body}"
            for pat in _FORBIDDEN_PATTERNS:
                if pat.search(blob):
                    items.append(
                        CritiqueItem(
                            severity="must",
                            category="fact",
                            target=target,
                            message=(
                                f"可见正文含禁止表述（匹配 {pat.pattern}）；"
                                "删除占比/commit/免责声明。"
                            ),
                        )
                    )
                    break
            for pat in _OVERCLAIM_PATTERNS:
                if pat.search(blob):
                    items.append(
                        CritiqueItem(
                            severity="must",
                            category="fact",
                            target=target,
                            message=(
                                "表述像整站/全栈个人主导；收窄为有 evidence 的具体模块"
                                "（负责 X 模块 / 实现 Y 功能），避免把他人工作算作自己的。"
                            ),
                        )
                    )
                    break
    return items


def _merge_must(
    hard: list[CritiqueItem],
    llm_must: list[CritiqueItem],
) -> list[CritiqueItem]:
    """硬过滤优先；LLM 意见按 target+message 去重追加。"""
    seen = {(i.target, i.message) for i in hard}
    merged = list(hard)
    for item in llm_must:
        key = (item.target, item.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item.model_copy(update={"severity": "must"}))
    return merged


def critique(
    *,
    draft: ResumeDraft,
    jd_text: str,
    fact_sheet: FactSheet | None = None,
    profile: SkillProfile | None = None,
    locale: str = "zh-CN",
    llm: LLMClient,
    max_retries: int = 2,
) -> CritiqueReport:
    """先程序化硬过滤，再 LLM 审稿；合并为 CritiqueReport。"""
    _ = profile
    hard = programmatic_checks(draft)
    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    template = env.get_template("resume/critic_review.j2")
    system = template.render(
        jd_text=jd_text.strip() or "(empty JD)",
        draft_json=draft.model_dump_json(indent=2),
        fact_sheet_block=fact_sheet.as_prompt_block() if fact_sheet is not None else "(empty)",
        hard_must_json=json.dumps(
            [i.model_dump() for i in hard],
            ensure_ascii=False,
            indent=2,
        ),
        locale=locale,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "按清单审稿，只输出 CritiqueReport JSON，不要重写简历。",
        },
    ]

    last_error: str | None = None
    critic_model = None
    cfg = getattr(llm, "_config", None)
    if cfg is not None and cfg.critic_model:
        critic_model = cfg.critic_model
    # schema 修一次即可；超时不重试（再等 120s 只会拖垮整单）
    for attempt in range(min(max_retries, 1) + 1):
        from repo2resume.agent.progress import emit_progress

        emit_progress(f"Critic：LLM 审稿（第 {attempt + 1}/{min(max_retries, 1) + 1} 次）…")
        try:
            result = llm.complete(
                messages,
                model=critic_model,
                temperature=0.1,
                use_cache=attempt == 0,
                timeout_s=90.0,
            )
        except TimeoutError as exc:
            last_error = str(exc)
            logger.warning("critique LLM timeout (attempt %s): %s", attempt + 1, exc)
            emit_progress("Critic：审稿超时，改用程序化硬过滤结果继续…")
            break
        try:
            payload = _extract_json(result.content)
            raw = CritiqueReport.model_validate_json(payload)
            must = _merge_must(hard, raw.must_fix)
            should = [i.model_copy(update={"severity": "should"}) for i in raw.should_fix]
            return CritiqueReport(must_fix=must, should_fix=should, passed=raw.passed)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning("critique validate failed (attempt %s): %s", attempt + 1, exc)
            messages.append({"role": "assistant", "content": result.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法通过 schema 校验：\n{last_error}\n请修复并只输出合法 JSON。"
                    ),
                }
            )

    # LLM 全挂/超时时仍返回硬过滤结果，避免整单 generate_resume 失败
    logger.error("critique LLM failed after retries: %s; returning hard checks only", last_error)
    return CritiqueReport(must_fix=hard, should_fix=[], passed=[])


def revise_experience(
    *,
    draft: ResumeDraft,
    report: CritiqueReport,
    jd_text: str,
    materials: list[SearchHit],
    profile: SkillProfile,
    fact_sheet: FactSheet | None = None,
    locale: str = "zh-CN",
    llm: LLMClient,
    include_should: bool = False,
    max_retries: int = 2,
) -> ResumeDraft:
    """按审稿意见修订；默认只消化 must_fix。"""
    _ = profile
    must = list(report.must_fix)
    should = list(report.should_fix) if include_should else []
    if not must and not should:
        return draft

    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    template = env.get_template("resume/revise_experience.j2")
    materials_payload = [
        {
            "doc_id": h.doc_id,
            "text": h.text,
            "score": h.score,
            "repo": _hit_repo(h),
            "metadata": h.metadata,
        }
        for h in materials
    ]
    system = template.render(
        jd_text=jd_text.strip() or "(empty JD)",
        draft_json=draft.model_dump_json(indent=2),
        must_fix_json=json.dumps([i.model_dump() for i in must], ensure_ascii=False, indent=2),
        should_fix_json=json.dumps([i.model_dump() for i in should], ensure_ascii=False, indent=2),
        include_should=str(include_should).lower(),
        materials_json=json.dumps(materials_payload, ensure_ascii=False, indent=2),
        fact_sheet_block=fact_sheet.as_prompt_block() if fact_sheet is not None else "(empty)",
        locale=locale,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "根据必须修改修订草稿，只输出 ResumeDraft JSON。",
        },
    ]

    last_error: str | None = None
    for attempt in range(max_retries + 1):
        result = llm.complete(
            messages,
            temperature=0.2,
            use_cache=attempt == 0,
            timeout_s=180.0,
        )
        try:
            payload = _extract_json(result.content)
            revised = ResumeDraft.model_validate_json(payload)
            revised.locale = locale
            revised.job_id = draft.job_id
            revised.job_title = draft.job_title
            if not revised.materials_used:
                revised.materials_used = list(draft.materials_used)
            revised.projects = sorted(revised.projects, key=lambda p: p.rank)
            fallback = _hit_repo(materials[0]) if materials else "unknown"
            return ensure_bullet_evidence(revised, fallback_repo=fallback)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning("revise_experience validate failed (attempt %s): %s", attempt + 1, exc)
            messages.append({"role": "assistant", "content": result.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法通过 schema 校验：\n{last_error}\n请修复并只输出合法 JSON。"
                    ),
                }
            )

    raise ValueError(f"revise_experience failed after retries: {last_error}")


def writer_critic_loop(
    *,
    jd_text: str,
    materials: list[SearchHit],
    profile: SkillProfile,
    fact_sheet: FactSheet | None = None,
    locale: str = "zh-CN",
    llm: LLMClient,
    max_rounds: int = 2,
    job_id: str | None = None,
    job_title: str | None = None,
) -> tuple[ResumeDraft, list[CritiqueReport]]:
    """write → critique → revise(must only)，最多 max_rounds 次审稿。"""
    from repo2resume.agent.progress import emit_progress

    if max_rounds < 1:
        raise ValueError("max_rounds must be >= 1")

    emit_progress("Writer：起草项目经历…")
    draft = write_experience(
        jd_text=jd_text,
        materials=materials,
        profile=profile,
        fact_sheet=fact_sheet,
        locale=locale,
        llm=llm,
        job_id=job_id,
        job_title=job_title,
    )
    reports: list[CritiqueReport] = []
    for round_i in range(max_rounds):
        emit_progress(f"Critic：审稿第 {round_i + 1}/{max_rounds} 轮…")
        report = critique(
            draft=draft,
            jd_text=jd_text,
            fact_sheet=fact_sheet,
            profile=profile,
            locale=locale,
            llm=llm,
        )
        reports.append(report)
        if report.approved:
            emit_progress("Critic：已通过")
            break
        if round_i >= max_rounds - 1:
            break
        emit_progress(f"Writer：按 must 修订（第 {round_i + 1} 轮）…")
        draft = revise_experience(
            draft=draft,
            report=report,
            jd_text=jd_text,
            materials=materials,
            profile=profile,
            fact_sheet=fact_sheet,
            locale=locale,
            llm=llm,
            include_should=False,
        )
    return draft, reports
