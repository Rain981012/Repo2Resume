"""【AI 生成】resume render / db draft / 轻量接线测试。"""

from __future__ import annotations

from pathlib import Path

from repo2resume.config import AppConfig
from repo2resume.resume.render import (
    render_project_experience,
    render_resume,
    write_markdown,
)
from repo2resume.storage.db import open_db
from repo2resume.storage.models import (
    BulletDraft,
    EvidenceRef,
    ProjectExperienceDraft,
    ResumeDraft,
    SkillProfile,
)


def _sample_draft() -> ResumeDraft:
    return ResumeDraft(
        job_title="后端工程师",
        projects=[
            ProjectExperienceDraft(
                project_name="社交分发平台",
                one_liner="去中心化内容分发服务。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="Inbox 收发",
                        body="实现 Inbox API 与签名校验；用 Redis 缓存热路径。",
                        evidence=EvidenceRef(source="social", detail="inbox"),
                    )
                ],
            )
        ],
    )


def test_render_project_experience_includes_src() -> None:
    md = render_project_experience(_sample_draft())
    assert "### 社交分发平台" in md
    assert "**Inbox 收发**" in md
    assert "<!-- src: social, inbox -->" in md


def test_render_multiple_bullets_are_separate_lines() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="社交分发平台",
                one_liner="去中心化内容分发服务。",
                bullets=[
                    BulletDraft(
                        label="认证",
                        body="实现 login。",
                        evidence=EvidenceRef(source="social", detail="auth"),
                    ),
                    BulletDraft(
                        label="Inbox",
                        body="实现 CRUD。",
                        evidence=EvidenceRef(source="social", detail="inbox"),
                    ),
                ],
            )
        ],
    )
    md = render_project_experience(draft)
    lines = [ln for ln in md.splitlines() if ln.startswith("- **")]
    assert len(lines) == 2
    assert all("<!-- src:" in ln for ln in lines)


def test_render_resume_with_profile_and_config() -> None:
    cfg = AppConfig(name="Rain", email="a@b.com", github="https://github.com/x")
    profile = SkillProfile(primary_direction="Python 后端")
    md = render_resume(_sample_draft(), profile=profile, config=cfg)
    assert "# Rain" in md
    assert "求职意向：后端工程师" in md
    assert "Python 后端" in md
    assert "### 社交分发平台" in md


def test_write_markdown_and_db_roundtrip(tmp_path: Path) -> None:
    draft = _sample_draft()
    path = write_markdown(tmp_path / "out.md", render_project_experience(draft))
    assert path.exists()
    assert "社交分发平台" in path.read_text(encoding="utf-8")

    db = open_db(tmp_path / "t.db")
    rid = db.save_resume_draft(draft, job_id="j1")
    loaded = db.load_latest_resume_draft()
    assert loaded is not None
    assert loaded[0] == rid
    assert loaded[1].projects[0].project_name == "社交分发平台"
    db.close()
