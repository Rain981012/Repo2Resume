"""简历 Markdown 渲染（Jinja2）与落盘。

【AI 生成】MVP：项目经历块 + 可选带头像信息的完整稿（技能来自画像，经历来自 ResumeDraft）。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from repo2resume.config import AppConfig
from repo2resume.storage.models import ResumeDraft, SkillProfile

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _format_project_block(project: object, *, strip_src: bool) -> str:
    """纯 Python 拼项目块，避免 Jinja trim_blocks 把 bullet 换行吃掉。"""
    name = getattr(project, "project_name", "")
    one_liner = getattr(project, "one_liner", "") or ""
    lines = [f"### {name}", "", one_liner, ""]
    for b in getattr(project, "bullets", []) or []:
        label = getattr(b, "label", "")
        body = getattr(b, "body", "")
        line = f"- **{label}**：{body}"
        if not strip_src:
            ev = getattr(b, "evidence", None)
            source = getattr(ev, "source", "") if ev is not None else ""
            detail = getattr(ev, "detail", None) if ev is not None else None
            src_tail = f"{source}, {detail}" if detail else source
            line = f"{line} <!-- src: {src_tail} -->"
        lines.append(line)
    return "\n".join(lines).rstrip()


def render_project_experience(draft: ResumeDraft, *, strip_src: bool = False) -> str:
    """只渲染项目经历 Markdown（对齐阶段 A 可复制块）。"""
    from repo2resume.resume.writer import ensure_bullet_evidence

    draft = ensure_bullet_evidence(draft)
    blocks = [_format_project_block(p, strip_src=strip_src) for p in draft.projects]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_resume(
    draft: ResumeDraft,
    *,
    profile: SkillProfile | None = None,
    config: AppConfig | None = None,
    strip_src: bool = False,
) -> str:
    """渲染含姓名/意向/技能摘要 + 项目经历的 Markdown。

    工作经历/教育 MVP 不填（无结构化来源）；投递前可手工补。
    """
    from repo2resume.resume.writer import ensure_bullet_evidence

    draft = ensure_bullet_evidence(draft)
    env = _env()
    template = env.get_template("resume.md.j2")

    name = (config.name if config else None) or "（姓名）"
    parts: list[str] = []
    if config:
        if config.email:
            parts.append(config.email)
        if config.github:
            parts.append(f"[GitHub]({config.github})")
    contact_line = " · ".join(parts)

    languages = ""
    tech_stack = ""
    primary = ""
    if profile is not None:
        primary = profile.primary_direction
        languages = "、".join(
            f"{lang.name} ({lang.share:.0%})" for lang in profile.coding_language[:6]
        )
        stack_bits: list[str] = []
        for field in ("languages", "frameworks", "databases", "tools_and_infra", "other"):
            items = getattr(profile.tech_stack, field, []) or []
            stack_bits.extend(items)
        tech_stack = "、".join(stack_bits[:12])

    projects_md = render_project_experience(draft, strip_src=strip_src).rstrip()

    return (
        template.render(
            name=name,
            contact_line=contact_line,
            target_title=draft.job_title,
            primary_direction=primary,
            languages=languages,
            tech_stack=tech_stack,
            projects_md=projects_md,
            strip_src=strip_src,
        ).rstrip()
        + "\n"
    )


def write_markdown(path: Path, content: str) -> Path:
    """写入 UTF-8 Markdown 文件，自动创建父目录。"""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
