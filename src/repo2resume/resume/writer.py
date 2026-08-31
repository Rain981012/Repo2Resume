"""简历 Writer：从检索素材选出项目，再生成带溯源的项目经历草稿。

【AI 辅助】已确认契约：
  - 方案 A：select_materials + write_experience → ResumeDraft
  - MVP 只出项目经历块；不调用 hybrid_search（hits 由调用方传入）
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from repo2resume.llm.client import LLMClient
from repo2resume.storage.models import (
    BulletDraft,
    EvidenceRef,
    FactSheet,
    ProjectExperienceDraft,
    ResumeDraft,
    SearchHit,
    SkillProfile,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


# LLM 偶尔把多条 bullet 塞进同一 body（「。 - **下一条**：…」），渲染会粘成一行
_FUSED_BULLET = re.compile(
    r"(?:^|\s)-\s+\*\*(?P<label>[^*]+?)\*\*[：:]\s*(?P<body>.*?)(?=(?:\s-\s+\*\*|$))",
    re.DOTALL,
)

_JOB_TITLE_NOISE = re.compile(r"(招聘|职位描述|岗位职责|任职要求|全职|兼职).*$")


def sanitize_job_title(title: str | None) -> str | None:
    """去掉搜岗摘要里粘在标题后的噪声词。"""
    if not title:
        return title
    cleaned = _JOB_TITLE_NOISE.sub("", title).strip(" -|/·")
    return cleaned or title.strip()


def expand_fused_bullets(bullets: list[BulletDraft]) -> list[BulletDraft]:
    """把粘在一起的 Markdown 列表拆回多条 BulletDraft。

    兼容：
      - 「。 - **下一条**：…」
      - 「。- **下一条**：…」（无空格）
      - body 内嵌完整 `- **标签**：`
    """
    out: list[BulletDraft] = []
    for bullet in bullets:
        # 规范化分隔：句号/分号后紧贴的 - ** 补空格，便于正则
        raw_body = bullet.body or ""
        raw_body = re.sub(r"([。；;])\s*-\s+\*\*", r"\1 - **", raw_body)
        blob = f"- **{bullet.label}**：{raw_body}"
        matches = list(_FUSED_BULLET.finditer(blob))
        if len(matches) <= 1:
            # 再试：仅按「 - **标签**：」切开（label 可能已丢）
            pieces = re.split(r"\s+-\s+\*\*([^*]+?)\*\*[：:]\s*", raw_body)
            if len(pieces) >= 3:
                # pieces: [body0, label1, body1, label2, body2, ...]
                first = pieces[0].strip()
                if first:
                    out.append(
                        BulletDraft(
                            label=bullet.label.strip(),
                            body=first,
                            evidence=bullet.evidence,
                        )
                    )
                for i in range(1, len(pieces), 2):
                    if i + 1 >= len(pieces):
                        break
                    out.append(
                        BulletDraft(
                            label=pieces[i].strip(),
                            body=pieces[i + 1].strip(),
                            evidence=bullet.evidence,
                        )
                    )
                continue
            out.append(bullet)
            continue
        for match in matches:
            out.append(
                BulletDraft(
                    label=match.group("label").strip(),
                    body=match.group("body").strip(),
                    evidence=bullet.evidence,
                )
            )
    return out


def ensure_bullet_evidence(
    draft: ResumeDraft,
    *,
    fallback_repo: str = "unknown",
) -> ResumeDraft:
    """空 evidence.source 时填 fallback，保证渲染始终有 <!-- src -->。"""
    projects: list[ProjectExperienceDraft] = []
    for project in draft.projects:
        bullets: list[BulletDraft] = []
        for bullet in expand_fused_bullets(project.bullets):
            source = (bullet.evidence.source or "").strip()
            detail = bullet.evidence.detail
            if not source:
                source = fallback_repo
                if not detail:
                    detail = "writer 未标注，已回填仓库名"
            bullets.append(
                BulletDraft(
                    label=bullet.label.strip(),
                    body=bullet.body.strip(),
                    evidence=EvidenceRef(source=source, detail=detail),
                )
            )
        projects.append(project.model_copy(update={"bullets": bullets}))
    return draft.model_copy(
        update={
            "projects": projects,
            "job_title": sanitize_job_title(draft.job_title),
        }
    )


def _hit_repo(hit: SearchHit) -> str:
    repo = hit.metadata.get("repo")
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    doc_id = hit.doc_id or ""
    if doc_id.startswith("repo:"):
        parts = doc_id.split(":")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()
    return "unknown"


def selected_repo_order(materials: list[SearchHit]) -> list[str]:
    """选材结果里仓库首次出现的顺序（primary 在 extras 之前）。"""
    order: list[str] = []
    for hit in materials:
        repo = _hit_repo(hit)
        if repo not in order:
            order.append(repo)
    return order


def estimate_repo_richness(
    materials: list[SearchHit],
    profile: SkillProfile | None = None,
) -> dict[str, int]:
    """估计每仓独立证据链条数（hit doc_id ∪ highlight claim，去重）。"""
    chains: dict[str, set[str]] = {}
    for hit in materials:
        repo = _hit_repo(hit)
        chains.setdefault(repo, set()).add(f"hit:{hit.doc_id}")
    if profile is not None:
        for highlight in profile.highlights_pool:
            repo = (highlight.repo or "").strip()
            claim = (highlight.claim or "").strip()
            if repo and claim:
                chains.setdefault(repo, set()).add(f"hl:{claim[:80]}")
    return {repo: len(ids) for repo, ids in chains.items()}


def _jd_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]*", text or ""):
        if len(tok) >= 2:
            tokens.add(tok.lower())
    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    for i in range(len(chars) - 1):
        tokens.add(chars[i] + chars[i + 1])
    return tokens


def _jd_overlap_score(jd_text: str, blob: str) -> float:
    jd_toks = _jd_tokens(jd_text)
    if not jd_toks:
        return 0.0
    blob_toks = _jd_tokens(blob)
    return len(jd_toks & blob_toks) / len(jd_toks)


def _repo_jd_blob(
    repo: str,
    hit: SearchHit,
    extras: list[SearchHit],
    profile: SkillProfile | None,
) -> str:
    parts = [hit.text or ""]
    for extra in extras:
        if _hit_repo(extra) == repo:
            parts.append(extra.text or "")
    if profile is not None:
        for one in profile.project_one_liners:
            if one.repo == repo:
                parts.append(one.summary or "")
        for highlight in profile.highlights_pool:
            if highlight.repo == repo:
                parts.append(highlight.claim or "")
    return "\n".join(parts)


def pack_materials_for_writer(
    materials: list[SearchHit],
    profile: SkillProfile,
    *,
    hard_exclude: set[str] | None = None,
    hints: dict[str, float | None] | None = None,
) -> list[dict]:
    """按仓打包：one_liner / highlights / 较长摘录 / evidence_chains。"""
    excluded = hard_exclude or set()
    order = [r for r in selected_repo_order(materials) if r not in excluded]
    richness = estimate_repo_richness(materials, profile)
    packed: list[dict] = []
    for repo in order:
        hits = [h for h in materials if _hit_repo(h) == repo]
        excerpts = [(h.text or "")[:1200] for h in hits[:2]]
        highlights = [
            h.claim[:240]
            for h in profile.highlights_pool
            if h.repo == repo and (h.claim or "").strip()
        ][:8]
        one_liner = next(
            (x.summary for x in profile.project_one_liners if x.repo == repo),
            "",
        )
        share = hints.get(repo) if hints else None
        if repo in (hints or {}) and hints is not None:
            note = (
                f"low_author_share={share}" if share is not None else "low_author_share=flagged"
            )
        else:
            note = "author_filtered_commits"
        packed.append(
            {
                "repo": repo,
                "one_liner": one_liner,
                "evidence_chains": richness.get(repo, 1),
                "highlights": highlights,
                "excerpts": excerpts,
                "authorship": note,
            }
        )
    return packed


def select_materials(
    *,
    jd_text: str,
    hits: list[SearchHit],
    profile: SkillProfile | None = None,
    max_projects: int = 4,
    preserve_hit_order: bool = False,
) -> list[SearchHit]:
    """按仓库去重并保留高分命中，限制项目数。

    默认剔除 author_share 极低的仓；其余低贡献仓排序靠后，减少误把他人工作写成主导。
    preserve_hit_order=True 时尊重传入顺序（精排结果），不再按 JD 词重叠重排。
    """
    from repo2resume.analysis.fact_sheet import (
        RESUME_EXCLUDE_SHARE_BELOW,
        authorship_hints_from_profile,
    )

    if max_projects <= 0 or not hits:
        return []

    hints: dict[str, float | None] = {}
    if profile is not None:
        hints = authorship_hints_from_profile(profile)

    hard_exclude = {
        repo
        for repo, share in hints.items()
        if share is not None and share < RESUME_EXCLUDE_SHARE_BELOW
    }
    soft_low = {
        repo for repo, share in hints.items() if share is None or share < 0.15
    } - hard_exclude

    filtered = [h for h in hits if _hit_repo(h) not in hard_exclude]
    # 若剔除后没有素材，回退到全量（避免空简历）
    work = filtered if filtered else list(hits)
    low_repos = soft_low if filtered else (soft_low | hard_exclude)

    ordered = work if preserve_hit_order else sorted(work, key=lambda h: h.score, reverse=True)
    best_by_repo: dict[str, SearchHit] = {}
    extras: list[SearchHit] = []
    seen: list[str] = []
    for hit in ordered:
        repo = _hit_repo(hit)
        if repo not in best_by_repo:
            best_by_repo[repo] = hit
            seen.append(repo)
        else:
            extras.append(hit)

    if preserve_hit_order:
        repos_ranked = sorted(seen, key=lambda r: (r in low_repos, seen.index(r)))
    else:
        overlap = {
            repo: _jd_overlap_score(jd_text, _repo_jd_blob(repo, hit, extras, profile))
            for repo, hit in best_by_repo.items()
        }
        # 低贡献靠后；同档按 JD 词重叠，再按检索分
        repos_ranked = sorted(
            best_by_repo.keys(),
            key=lambda r: (r in low_repos, -overlap[r], -best_by_repo[r].score),
        )
    selected_repos = repos_ranked[:max_projects]
    selected_set = set(selected_repos)
    primary = [best_by_repo[r] for r in selected_repos]
    more = [h for h in extras if _hit_repo(h) in selected_set]
    return primary + more


def write_experience(
    *,
    jd_text: str,
    materials: list[SearchHit],
    profile: SkillProfile,
    fact_sheet: FactSheet | None = None,
    locale: str = "zh-CN",
    llm: LLMClient,
    job_id: str | None = None,
    job_title: str | None = None,
    max_retries: int = 1,
    use_cache: bool | None = None,
) -> ResumeDraft:
    """调用 LLM 生成 ResumeDraft；校验失败则把错误回传重试。"""
    from repo2resume.agent.progress import emit_progress

    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    template = env.get_template("resume/write_experience.j2")
    # 控制 prompt 体积：全量 profile + 长素材会让 glm 极慢甚至看似卡死
    from repo2resume.analysis.fact_sheet import (
        RESUME_EXCLUDE_SHARE_BELOW,
        authorship_hints_from_profile,
        fact_sheet_from_profile,
    )

    if fact_sheet is None:
        fact_sheet = fact_sheet_from_profile(profile)

    hints = authorship_hints_from_profile(profile)
    hard_exclude = {
        repo
        for repo, share in hints.items()
        if share is not None and share < RESUME_EXCLUDE_SHARE_BELOW
    }

    materials_payload = pack_materials_for_writer(
        materials,
        profile,
        hard_exclude=hard_exclude,
        hints=hints,
    )
    profile_slim = {
        "primary_direction": profile.primary_direction,
        "secondary_directions": profile.secondary_directions[:5],
        "tech_stack": profile.tech_stack.model_dump(),
        "project_one_liners": [
            x.model_dump() for x in profile.project_one_liners[:8] if x.repo not in hard_exclude
        ],
        "highlights_pool": [
            {"repo": h.repo, "claim": h.claim[:200]}
            for h in profile.highlights_pool[:12]
            if h.repo not in hard_exclude
        ],
        # 低贡献仓警示：写作时必须收窄表述，不得写成整站主导
        "caution": [c if isinstance(c, str) else str(c) for c in profile.caution[:8]],
        "authorship_hints": {k: (v if v is not None else "flagged") for k, v in hints.items()},
    }
    system = template.render(
        locale=locale,
        jd_text=(jd_text.strip() or "(empty JD)")[:4000],
        materials_json=json.dumps(materials_payload, ensure_ascii=False, indent=2),
        profile_json=json.dumps(profile_slim, ensure_ascii=False, indent=2),
        fact_sheet_block=(
            fact_sheet.as_prompt_block()[:3000] if fact_sheet is not None else "(empty)"
        ),
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "根据 system 中的 JD / 素材 / 画像 / 事实清单，输出项目经历 JSON。"
                "只输出 JSON 对象，不要 Markdown。"
                "projects 顺序必须等于素材仓库顺序；"
                "rank 0 且 evidence_chains≥4 时写 4–5 条独立模块，"
                "禁止抄 few-shot 的单条形态。"
                "严格只写本人提交能支撑的模块级贡献；caution 里的低贡献仓不得写成整站主导。"
            ),
        },
    ]

    writer_model = None
    cfg = getattr(llm, "_config", None)
    if cfg is not None:
        writer_model = cfg.complete_model("writer")
    writer_shown = writer_model or (cfg.llm_model if cfg is not None else "default")

    last_error: str | None = None
    for attempt in range(max_retries + 1):
        emit_progress(
            f"Writer：用 {writer_shown} 起草"
            f"（第 {attempt + 1}/{max_retries + 1} 次，最长约 3 分钟）…"
        )
        cache_hit = (attempt == 0) if use_cache is None else use_cache
        result = llm.complete(
            messages,
            model=writer_model,
            temperature=0.2,
            use_cache=cache_hit,
            timeout_s=180.0,
            role="writer",
        )
        try:
            payload = _extract_json(result.content)
            draft = ResumeDraft.model_validate_json(payload)
            if job_id is not None:
                draft.job_id = job_id
            if job_title is not None:
                draft.job_title = job_title
            draft.locale = locale
            if not draft.materials_used:
                draft.materials_used = [h.doc_id for h in materials]
            draft.projects = sorted(draft.projects, key=lambda p: p.rank)
            fallback = _hit_repo(materials[0]) if materials else "unknown"
            return ensure_bullet_evidence(draft, fallback_repo=fallback)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning("write_experience validate failed (attempt %s): %s", attempt + 1, exc)
            messages.append({"role": "assistant", "content": result.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法通过 schema 校验：\n{last_error}\n请修复并只输出合法 JSON。"
                    ),
                }
            )

    raise ValueError(f"write_experience failed after retries: {last_error}")


def draft_to_markdown(draft: ResumeDraft) -> str:
    """把 ResumeDraft 渲染成可复制的项目经历 Markdown（含 <!-- src -->）。"""
    blocks: list[str] = []
    for project in draft.projects:
        lines = [f"### {project.project_name}", "", project.one_liner, ""]
        for bullet in project.bullets:
            src = bullet.evidence.source
            detail = bullet.evidence.detail
            src_tail = f"{src}, {detail}" if detail else src
            lines.append(f"- **{bullet.label}**：{bullet.body} <!-- src: {src_tail} -->")
        blocks.append("\n".join(lines).rstrip())
    return "\n\n".join(blocks) + ("\n" if blocks else "")
