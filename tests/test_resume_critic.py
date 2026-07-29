"""【AI 辅助】resume Critic：硬过滤 + critique + revise + loop。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from repo2resume.resume.critic import (
    critique,
    programmatic_checks,
    revise_experience,
    writer_critic_loop,
)
from repo2resume.storage.models import (
    BulletDraft,
    CritiqueItem,
    CritiqueReport,
    EvidenceRef,
    ProjectExperienceDraft,
    ResumeDraft,
    SearchHit,
    SkillProfile,
)


def _draft_ok() -> ResumeDraft:
    return ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="社交平台",
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
        ]
    )


def _draft_bad() -> ResumeDraft:
    return ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="order-service（多人协作）",
                one_liner="本人贡献占比低的订单服务。",
                bullets=[
                    BulletDraft(
                        label="",
                        body="负责后端开发",
                        evidence=EvidenceRef(source=""),
                    )
                ],
            )
        ]
    )


def test_programmatic_checks_flags_forbidden_and_empty_evidence() -> None:
    items = programmatic_checks(_draft_bad())
    assert items
    assert all(i.severity == "must" for i in items)
    messages = " ".join(i.message for i in items)
    assert "evidence.source" in messages or "禁止" in messages


def test_programmatic_checks_flags_overclaim_wording() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="社交平台",
                one_liner="分布式社交。",
                bullets=[
                    BulletDraft(
                        label="全栈",
                        body="设计并实现完整的用户认证系统与跨节点通信。",
                        evidence=EvidenceRef(source="social", detail="auth"),
                    )
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any("整站" in i.message or "模块" in i.message for i in items)


def test_critique_merges_hard_checks_with_llm() -> None:
    llm = MagicMock()
    llm.complete.return_value = MagicMock(
        content=json.dumps(
            {
                "must_fix": [
                    {
                        "severity": "must",
                        "category": "style",
                        "target": "社交平台 / 第1条",
                        "message": "略短，建议扩到 80 字。",
                    }
                ],
                "should_fix": [
                    {
                        "severity": "should",
                        "category": "ats",
                        "target": "社交平台",
                        "message": "可补 Redis 关键词。",
                    }
                ],
                "passed": ["有 src"],
            },
            ensure_ascii=False,
        )
    )
    # 干净草稿 → 硬过滤空，LLM must 保留
    report = critique(draft=_draft_ok(), jd_text="Redis backend", llm=llm)
    assert len(report.must_fix) == 1
    assert report.should_fix[0].category == "ats"
    assert report.approved is False


def test_critique_hard_only_when_llm_fails() -> None:
    llm = MagicMock()
    llm.complete.return_value = MagicMock(content="{bad")
    report = critique(draft=_draft_bad(), jd_text="x", llm=llm, max_retries=0)
    assert report.must_fix
    assert report.approved is False


def test_critique_timeout_falls_back_to_hard_checks() -> None:
    """超时不应炸穿 generate_resume，应退回硬过滤。"""
    llm = MagicMock()
    llm.complete.side_effect = TimeoutError("LLM 调用超过 90s")
    report = critique(draft=_draft_bad(), jd_text="x", llm=llm, max_retries=2)
    assert report.must_fix
    assert report.approved is False
    llm.complete.assert_called_once()


def test_revise_skips_when_no_must() -> None:
    llm = MagicMock()
    draft = _draft_ok()
    report = CritiqueReport(
        must_fix=[],
        should_fix=[
            CritiqueItem(
                severity="should",
                category="ats",
                target="x",
                message="可选",
            )
        ],
    )
    out = revise_experience(
        draft=draft,
        report=report,
        jd_text="x",
        materials=[],
        profile=SkillProfile(primary_direction="Python"),
        llm=llm,
        include_should=False,
    )
    assert out is draft
    llm.complete.assert_not_called()


def test_writer_critic_loop_stops_when_approved() -> None:
    good = {
        "locale": "zh-CN",
        "projects": [
            {
                "project_name": "P",
                "one_liner": "产品。",
                "rank": 0,
                "bullets": [
                    {
                        "label": "模块实现",
                        "body": "完成核心接口与缓存；提升稳定性与可观测性。",
                        "evidence": {"source": "r", "detail": "t"},
                    }
                ],
            }
        ],
        "materials_used": ["1"],
    }
    critique_ok = {
        "must_fix": [],
        "should_fix": [],
        "passed": ["ok"],
    }
    llm = MagicMock()
    llm.complete.side_effect = [
        MagicMock(content=json.dumps(good, ensure_ascii=False)),
        MagicMock(content=json.dumps(critique_ok, ensure_ascii=False)),
    ]
    materials = [
        SearchHit(
            doc_id="1",
            text="x",
            score=1.0,
            rank=1,
            source="vector",
            metadata={"repo": "r"},
        )
    ]
    draft, reports = writer_critic_loop(
        jd_text="Python",
        materials=materials,
        profile=SkillProfile(primary_direction="Python"),
        llm=llm,
        max_rounds=2,
    )
    assert draft.projects[0].project_name == "P"
    assert len(reports) == 1
    assert reports[0].approved
    assert llm.complete.call_count == 2  # write + critique，无 revise


def test_max_rounds_rejects_zero() -> None:
    with pytest.raises(ValueError):
        writer_critic_loop(
            jd_text="x",
            materials=[],
            profile=SkillProfile(primary_direction="x"),
            llm=MagicMock(),
            max_rounds=0,
        )
