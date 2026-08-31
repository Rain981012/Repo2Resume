"""【AI 辅助】resume Writer：select_materials + write_experience。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from repo2resume.resume.writer import (
    draft_to_markdown,
    ensure_bullet_evidence,
    estimate_repo_richness,
    expand_fused_bullets,
    pack_materials_for_writer,
    sanitize_job_title,
    select_materials,
    selected_repo_order,
    write_experience,
)
from repo2resume.storage.models import (
    BulletDraft,
    EvidenceRef,
    FactSheet,
    Highlight,
    ProjectExperienceDraft,
    ProjectOneLiner,
    ResumeDraft,
    SearchHit,
    SkillProfile,
)


def _hit(doc_id: str, repo: str, score: float, text: str = "material") -> SearchHit:
    return SearchHit(
        doc_id=doc_id,
        text=text,
        score=score,
        rank=1,
        source="vector",
        metadata={"repo": repo},
    )


def _profile() -> SkillProfile:
    return SkillProfile(primary_direction="Python 后端")


def test_select_materials_dedupes_by_repo_and_caps_projects() -> None:
    hits = [
        _hit("a1", "repo-a", 0.9),
        _hit("b1", "repo-b", 0.8),
        _hit("a2", "repo-a", 0.7),
        _hit("c1", "repo-c", 0.6),
        _hit("d1", "repo-d", 0.5),
    ]
    selected = select_materials(jd_text="Python", hits=hits, max_projects=2)
    repos = {h.metadata["repo"] for h in selected}
    assert repos == {"repo-a", "repo-b"}
    # 同仓次优命中也保留
    assert {h.doc_id for h in selected} >= {"a1", "b1", "a2"}


def test_select_materials_empty() -> None:
    assert select_materials(jd_text="x", hits=[], max_projects=4) == []


def test_select_materials_excludes_very_low_author_share() -> None:
    profile = SkillProfile(
        primary_direction="Python",
        caution=[
            "NLP_GAME: author_share=0.111 < 0.15，勿写成个人主导",
            "good-repo: author_share=0.40",
        ],
    )
    hits = [
        _hit("n1", "NLP_GAME", 0.99),
        _hit("g1", "good-repo", 0.5),
        _hit("o1", "other", 0.4),
    ]
    selected = select_materials(jd_text="Python", hits=hits, profile=profile, max_projects=3)
    repos = {_hit_repo_name(h) for h in selected}
    assert "NLP_GAME" not in repos
    assert "good-repo" in repos


def _hit_repo_name(hit: SearchHit) -> str:
    repo = hit.metadata.get("repo")
    return repo if isinstance(repo, str) else "unknown"


def test_write_experience_parses_llm_json() -> None:
    payload = {
        "locale": "zh-CN",
        "projects": [
            {
                "project_name": "社交分发平台",
                "one_liner": "去中心化社交内容分发服务。",
                "rank": 0,
                "bullets": [
                    {
                        "label": "Inbox 收发",
                        "body": "实现 Inbox API 与签名校验；用 Redis 缓存热路径降低延迟。",
                        "evidence": {"source": "socialdistribution", "detail": "highlight inbox"},
                    }
                ],
            }
        ],
        "materials_used": [],
    }
    llm = MagicMock()
    llm.complete.return_value = MagicMock(content=json_dumps(payload))

    materials = [_hit("d1", "socialdistribution", 0.9, "Inbox API Redis")]
    draft = write_experience(
        jd_text="Python backend Redis",
        materials=materials,
        profile=_profile(),
        fact_sheet=FactSheet(entries=[]),
        llm=llm,
        job_title="Backend Engineer",
    )
    assert isinstance(draft, ResumeDraft)
    assert draft.job_title == "Backend Engineer"
    assert draft.materials_used == ["d1"]
    assert draft.projects[0].bullets[0].evidence.source == "socialdistribution"
    md = draft_to_markdown(draft)
    assert "### 社交分发平台" in md
    assert "<!-- src: socialdistribution, highlight inbox -->" in md


def test_write_experience_retries_on_bad_json() -> None:
    good = {
        "locale": "zh-CN",
        "projects": [
            {
                "project_name": "P",
                "one_liner": "产品定位。",
                "rank": 0,
                "bullets": [
                    {
                        "label": "模块实现",
                        "body": "完成核心接口与测试；提升稳定性。",
                        "evidence": {"source": "r", "detail": "t"},
                    }
                ],
            }
        ],
    }
    llm = MagicMock()
    llm.complete.side_effect = [
        MagicMock(content="not-json"),
        MagicMock(content=json_dumps(good)),
    ]
    draft = write_experience(
        jd_text="x",
        materials=[_hit("1", "r", 1.0)],
        profile=_profile(),
        llm=llm,
        max_retries=2,
    )
    assert draft.projects[0].project_name == "P"
    assert llm.complete.call_count == 2


def test_write_experience_raises_after_retries() -> None:
    llm = MagicMock()
    llm.complete.return_value = MagicMock(content="{bad")
    with pytest.raises(ValueError, match="write_experience failed"):
        write_experience(
            jd_text="x",
            materials=[_hit("1", "r", 1.0)],
            profile=_profile(),
            llm=llm,
            max_retries=1,
        )


def json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def test_expand_fused_bullets_splits_embedded_markdown() -> None:
    fused = BulletDraft(
        label="认证模块开发",
        body=(
            "实现 signup/login。"
            " - **Inbox API 开发**：负责 inbox CRUD。"
            " - **API 文档与测试**：引入 Swagger。"
        ),
        evidence=EvidenceRef(source="social", detail="auth"),
    )
    parts = expand_fused_bullets([fused])
    assert len(parts) == 3
    assert parts[0].label == "认证模块开发"
    assert parts[1].label == "Inbox API 开发"
    assert parts[2].label == "API 文档与测试"
    assert all(p.evidence.source == "social" for p in parts)


def test_ensure_bullet_evidence_fills_empty_source_and_title() -> None:
    draft = ResumeDraft(
        job_title="Python 后端开发工程师招聘全职",
        projects=[
            ProjectExperienceDraft(
                project_name="X",
                one_liner="产品。",
                bullets=[
                    BulletDraft(
                        label="A",
                        body="做了 A。",
                        evidence=EvidenceRef(source=""),
                    )
                ],
            )
        ],
    )
    fixed = ensure_bullet_evidence(draft, fallback_repo="Repo2Resume")
    assert fixed.job_title == "Python 后端开发工程师"
    assert fixed.projects[0].bullets[0].evidence.source == "Repo2Resume"


def test_sanitize_job_title() -> None:
    assert sanitize_job_title("Python 后端开发工程师招聘") == "Python 后端开发工程师"


def test_select_materials_ranks_by_jd_overlap() -> None:
    hits = [
        _hit("f1", "pdf-ui", 0.99, "React 前端 PDF 评论 登录页面 上传"),
        _hit("b1", "social-backend", 0.50, "Django authors inbox 后端 API PostgreSQL"),
    ]
    selected = select_materials(
        jd_text="后端开发实习生 Python Django",
        hits=hits,
        max_projects=2,
    )
    order: list[str] = []
    for hit in selected:
        repo = hit.metadata["repo"]
        if repo not in order:
            order.append(str(repo))
    assert order[0] == "social-backend"


def test_select_materials_preserve_hit_order() -> None:
    hits = [
        _hit("f1", "pdf-ui", 0.99, "React 前端 PDF 评论 登录页面 上传"),
        _hit("b1", "social-backend", 0.50, "Django authors inbox 后端 API PostgreSQL"),
    ]
    selected = select_materials(
        jd_text="后端开发实习生 Python Django",
        hits=hits,
        max_projects=2,
        preserve_hit_order=True,
    )
    order: list[str] = []
    for hit in selected:
        repo = hit.metadata["repo"]
        if repo not in order:
            order.append(str(repo))
    assert order[0] == "pdf-ui"


def test_select_materials_infers_repo_from_doc_id() -> None:
    hits = [
        SearchHit(
            doc_id="repo:Repo2Resume:summary",
            text="agent loop RAG",
            score=0.9,
            rank=1,
            source="hybrid",
            metadata={},
        ),
        SearchHit(
            doc_id="repo:socialdistribution:summary",
            text="django inbox",
            score=0.8,
            rank=2,
            source="hybrid",
            metadata={},
        ),
    ]
    selected = select_materials(jd_text="agent RAG", hits=hits, max_projects=2)
    assert selected_repo_order(selected)[:2] == ["Repo2Resume", "socialdistribution"]


def test_pack_materials_sets_evidence_chains() -> None:
    profile = SkillProfile(
        primary_direction="Python",
        highlights_pool=[
            Highlight(
                repo="r",
                claim="agent loop",
                evidence=EvidenceRef(source="r", detail="a"),
            ),
            Highlight(
                repo="r",
                claim="hybrid search",
                evidence=EvidenceRef(source="r", detail="b"),
            ),
        ],
        project_one_liners=[ProjectOneLiner(repo="r", summary="CLI 简历工具")],
    )
    materials = [_hit("1", "r", 0.9, "loop"), _hit("2", "r", 0.8, "search")]
    packed = pack_materials_for_writer(materials, profile)
    assert packed[0]["repo"] == "r"
    assert packed[0]["evidence_chains"] >= 4
    assert packed[0]["one_liner"] == "CLI 简历工具"
    assert estimate_repo_richness(materials, profile)["r"] >= 4
