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
    estimate_repo_richness,
    pack_materials_for_writer,
    selected_repo_order,
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
        r"独立开发.{0,80}完整",
        r"完整\s*pipeline",
        r"设计并实现完整",
        r"构建了完整的.{0,12}系统",
        r"开发了全部",
        r"负责整个系统",
        r"从零搭建整[个套]",
    )
)

# one_liner 只写产品定位；命中职责词 → must
_ONELINER_DUTY = re.compile(r"负责|本人|实现了|独立开发|贡献")

_LABEL_MIN_CHARS = 4
_LABEL_MAX_CHARS = 12

# 对齐 skill/prompts/03_resume_writing.md：约 80–180 汉字；合并同链时可略超
_MIN_BODY_CHARS = 80
_MAX_BODY_CHARS = 280

CRITIC_INCOMPLETE_MUST = "审稿未完成，按 write_experience 硬性规则自检修订"
_CRITIC_TIMEOUT_S = 240.0
_REVISE_TIMEOUT_S = 300.0


def _visible_char_len(text: str) -> int:
    """去空白后的可见字符数（中文简历密度用）。"""
    return len(re.sub(r"\s+", "", text or ""))


def _project_source(project: object) -> str:
    bullets = getattr(project, "bullets", None) or []
    for bullet in bullets:
        source = (getattr(getattr(bullet, "evidence", None), "source", None) or "").strip()
        if source:
            return source
    return ""


def critic_incomplete_item() -> CritiqueItem:
    return CritiqueItem(
        severity="must",
        category="structure",
        target="全稿",
        message=CRITIC_INCOMPLETE_MUST,
    )


def format_generate_resume_status(reports: list[CritiqueReport]) -> str:
    """generate_resume / CLI 用的审稿状态句；超时未审不得写成已通过。"""
    if not reports:
        return "无审稿记录"
    last = reports[-1]
    if not last.critic_complete:
        return "Critic 未完成审稿，已按模板自检修订，请人工过一遍"
    if last.approved:
        return "Critic 已通过"
    return (
        f"Critic 仍有 {len(last.must_fix)} 条 must（草稿已落盘，可稍后自然语言改；"
        "不要再次调用 generate_resume）"
    )


def _order_is_subsequence(actual: list[str], expected: list[str]) -> bool:
    allowed = set(expected)
    if any(repo not in allowed for repo in actual):
        return False
    cursor = iter(expected)
    return all(repo in cursor for repo in actual)


def programmatic_checks(
    draft: ResumeDraft,
    *,
    richness: dict[str, int] | None = None,
    expected_repo_order: list[str] | None = None,
) -> list[CritiqueItem]:
    """确定性检查：空 evidence / 空正文 / 禁词 / 口号式过短 / 条数。在 LLM critique 之前调用。"""
    items: list[CritiqueItem] = []
    if expected_repo_order:
        actual_order = [_project_source(p) for p in draft.projects]
        actual_order = [r for r in actual_order if r]
        if actual_order and not _order_is_subsequence(actual_order, expected_repo_order):
            items.append(
                CritiqueItem(
                    severity="must",
                    category="ats",
                    target="全稿",
                    message=(
                        "项目顺序与选材仓库顺序不一致；"
                        f"当前 {[_project_source(p) or p.project_name for p in draft.projects]}，"
                        f"应为 {list(expected_repo_order)} 的相对序。"
                    ),
                )
            )

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
        if _ONELINER_DUTY.search(project.one_liner or ""):
            items.append(
                CritiqueItem(
                    severity="must",
                    category="style",
                    target=project.project_name,
                    message=(
                        "one_liner 含职责措辞（负责/本人/实现了/独立开发/贡献）；"
                        "只写产品/业务定位。"
                    ),
                )
            )

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

        n_bullets = len(project.bullets)
        sole = project.bullets[0] if n_bullets == 1 else None
        sole_dense = (
            sole is not None and _visible_char_len(f"{sole.label}{sole.body}") >= _MIN_BODY_CHARS
        )
        repo = _project_source(project)
        chain_n = richness.get(repo) if richness is not None and repo else None
        if chain_n is not None and chain_n >= 4 and project.rank == 0:
            if n_bullets < 4 or n_bullets > 5:
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="structure",
                        target=project.project_name,
                        message=(
                            f"主项目素材充实（evidence_chains≈{chain_n}）"
                            "须写 4–5 条独立模块 bullet，"
                            f"当前 {n_bullets} 条；禁止把整条 pipeline 揉进一条。"
                        ),
                    )
                )
        elif chain_n is not None and chain_n >= 2:
            if n_bullets < 2:
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="structure",
                        target=project.project_name,
                        message=(
                            f"该仓至少 2 条有证据的独立模块（evidence_chains≈{chain_n}）；"
                            "1 条密度段仅允许同证据链且素材很少时使用。"
                        ),
                    )
                )
        elif n_bullets < 2 and not sole_dense:
            items.append(
                CritiqueItem(
                    severity="must",
                    category="structure",
                    target=project.project_name,
                    message=(
                        "经历条数不足：相关较弱也至少 2 条有证据的 bullet；"
                        "若确属同一功能/证据链可只保留 1 条，但须写成约 80–180 字密度 STAR，"
                        "禁止口号式半句。证据不够则移出本稿。"
                    ),
                )
            )

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
                continue
            label_len = _visible_char_len(bullet.label)
            if label_len < _LABEL_MIN_CHARS or label_len > _LABEL_MAX_CHARS:
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="style",
                        target=target,
                        message=(
                            f"短标签须 4–12 字（当前约 {label_len} 字）；"
                            "改成动作/模块名，勿写成整句职责。"
                        ),
                    )
                )
            body_len = _visible_char_len(f"{bullet.label}{bullet.body}")
            if body_len < _MIN_BODY_CHARS:
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="style",
                        target=target,
                        message=(
                            f"正文过短（约 {body_len} 字，要求 ≥{_MIN_BODY_CHARS}）："
                            "禁止口号式半句；按 STAR 扩成密度技术叙述"
                            "（动作+技术栈；分号衔接结果），约 80–180 字。"
                        ),
                    )
                )
            elif body_len > _MAX_BODY_CHARS:
                items.append(
                    CritiqueItem(
                        severity="must",
                        category="style",
                        target=target,
                        message=(
                            f"正文过长（约 {body_len} 字，上限约 {_MAX_BODY_CHARS}）："
                            "压缩成简历式分号衔接，勿写成议论文叙事段落。"
                        ),
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
    richness: dict[str, int] | None = None,
    expected_repo_order: list[str] | None = None,
    use_cache: bool | None = None,
) -> CritiqueReport:
    """先程序化硬过滤，再 LLM 审稿；合并为 CritiqueReport。"""
    _ = profile
    hard = programmatic_checks(draft, richness=richness, expected_repo_order=expected_repo_order)
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
    if cfg is not None:
        critic_model = cfg.complete_model("critic")
    critic_shown = critic_model or (cfg.llm_model if cfg is not None else "default")
    n_attempts = 2 if max_retries >= 1 else 1
    for attempt in range(n_attempts):
        from repo2resume.agent.progress import emit_progress

        emit_progress(f"Critic：用 {critic_shown} 审稿（第 {attempt + 1}/{n_attempts} 次）…")
        try:
            cache_hit = (attempt == 0) if use_cache is None else use_cache
            result = llm.complete(
                messages,
                model=critic_model,
                temperature=0.1,
                use_cache=cache_hit,
                timeout_s=_CRITIC_TIMEOUT_S,
                role="critic",
            )
        except TimeoutError as exc:
            last_error = str(exc)
            logger.warning("critique LLM timeout (attempt %s): %s", attempt + 1, exc)
            if attempt + 1 < n_attempts:
                emit_progress("Critic：审稿超时，再试一次…")
            else:
                emit_progress("Critic：审稿再次超时，将按模板自检修订…")
            continue
        try:
            payload = _extract_json(result.content)
            raw = CritiqueReport.model_validate_json(payload)
            must = _merge_must(hard, raw.must_fix)
            should = [i.model_copy(update={"severity": "should"}) for i in raw.should_fix]
            return CritiqueReport(
                must_fix=must,
                should_fix=should,
                passed=raw.passed,
                critic_complete=True,
            )
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

    logger.error("critique LLM failed after retries: %s; injecting self-check must", last_error)
    must = _merge_must(hard, [critic_incomplete_item()])
    return CritiqueReport(must_fix=must, should_fix=[], passed=[], critic_complete=False)


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
    from repo2resume.analysis.fact_sheet import (
        RESUME_EXCLUDE_SHARE_BELOW,
        authorship_hints_from_profile,
    )

    must = list(report.must_fix)
    should = list(report.should_fix) if include_should else []
    if not must and not should:
        return draft

    hints = authorship_hints_from_profile(profile)
    hard_exclude = {
        repo
        for repo, share in hints.items()
        if share is not None and share < RESUME_EXCLUDE_SHARE_BELOW
    }
    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    template = env.get_template("resume/revise_experience.j2")
    materials_payload = pack_materials_for_writer(
        materials,
        profile,
        hard_exclude=hard_exclude,
        hints=hints,
    )
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
            "content": (
                "根据必须修改修订草稿，只输出 ResumeDraft JSON。"
                "继续遵守 write_experience 硬性规则：label 4–12 字、主项目 4–5 条、"
                "顺序与素材仓库一致、禁止口号与整链贪功。"
            ),
        },
    ]

    last_error: str | None = None
    writer_model = None
    cfg = getattr(llm, "_config", None)
    if cfg is not None:
        writer_model = cfg.complete_model("writer")
    for attempt in range(max_retries + 1):
        result = llm.complete(
            messages,
            model=writer_model,
            temperature=0.2,
            use_cache=attempt == 0,
            timeout_s=_REVISE_TIMEOUT_S,
            role="revise",
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
    use_cache: bool | None = None,
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
        use_cache=use_cache,
    )
    richness = estimate_repo_richness(materials, profile)
    expected_order = selected_repo_order(materials)
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
            richness=richness,
            expected_repo_order=expected_order,
            use_cache=use_cache,
        )
        reports.append(report)
        if report.approved:
            emit_progress("Critic：已通过")
            break
        if round_i >= max_rounds - 1:
            break
        if not report.critic_complete:
            emit_progress("Critic：未完成审稿，按模板自检修订…")
        else:
            emit_progress(f"Writer：按 must 修订（第 {round_i + 1} 轮）…")
        try:
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
        except (TimeoutError, ValueError) as exc:
            logger.warning("revise_experience failed, keeping current draft: %s", exc)
            emit_progress("Writer：自检修订失败，保留当前草稿并落盘…")
            break
    return draft, reports
