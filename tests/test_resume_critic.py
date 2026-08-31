"""【AI 辅助】resume Critic：硬过滤 + critique + revise + loop。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from repo2resume.resume.critic import (
    CRITIC_INCOMPLETE_MUST,
    critique,
    format_generate_resume_status,
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

_DENSE_INBOX = (
    "实现 Inbox 收发接口与跨节点请求签名校验；使用 Redis 缓存热门时间线以降低跨节点拉取延迟；"
    "补充批次失败重试与操作审计日志，避免静默丢件并便于定位投递故障。"
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
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="social", detail="inbox"),
                    ),
                    BulletDraft(
                        label="关注关系",
                        body=(
                            "实现关注与取关写路径、粉丝列表分页及本地缓存失效；"
                            "保证列表与时间线最终一致；对下游超时与错误响应做降级，"
                            "避免单点故障拖垮主流程；补充操作审计日志便于排查误操作。"
                        ),
                        evidence=EvidenceRef(source="social", detail="follow"),
                    ),
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
                        body=(
                            "设计并实现完整的用户认证系统与跨节点通信，"
                            "覆盖注册登录会话与消息链路的全部前后端模块并独立上线运维。"
                        ),
                        evidence=EvidenceRef(source="social", detail="auth"),
                    )
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any("整站" in i.message or "模块" in i.message for i in items)


def test_programmatic_checks_flags_slogan_short_body() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="天气预测",
                one_liner="气象预测系统。",
                bullets=[
                    BulletDraft(
                        label="模型训练",
                        body="完成模型训练与评估部分",
                        evidence=EvidenceRef(source="ada", detail="train"),
                    ),
                    BulletDraft(
                        label="特征",
                        body="添加降雨量预测",
                        evidence=EvidenceRef(source="ada", detail="rain"),
                    ),
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any(i.category == "style" and "过短" in i.message for i in items)


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
    assert report.critic_complete is False


def test_critique_timeout_retries_then_injects_self_check() -> None:
    """超时再试 1 次；仍失败则注入自检 must，不得当作通过。"""
    llm = MagicMock()
    llm.complete.side_effect = TimeoutError("LLM 调用超过 90s")
    report = critique(draft=_draft_ok(), jd_text="x", llm=llm, max_retries=2)
    assert report.critic_complete is False
    assert report.approved is False
    assert any(CRITIC_INCOMPLETE_MUST in i.message for i in report.must_fix)
    assert llm.complete.call_count == 2


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
                        "body": (
                            "实现核心读写接口与本地缓存失效策略；补充超时重试、"
                            "结构化日志与关键指标导出，提升故障可观测性；"
                            "对下游错误做降级处理，避免拖垮主请求链路，并保留排障上下文。"
                        ),
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


def test_programmatic_checks_flags_label_length() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="简历 CLI",
                one_liner="本地仓库分析生成 Markdown 简历。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="Git仓库分析与简历草稿生成",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="resume-cli", detail="pipeline"),
                    ),
                    BulletDraft(
                        label="测试与 CI",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="resume-cli", detail="ci"),
                    ),
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any("4–12" in i.message or "4-12" in i.message for i in items)


def test_programmatic_checks_flags_rich_primary_too_few_bullets() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="简历 CLI",
                one_liner="本地仓库分析生成 Markdown 简历。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="仓库分析流水线",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="resume-cli", detail="all"),
                    )
                ],
            )
        ]
    )
    items = programmatic_checks(draft, richness={"resume-cli": 4})
    assert any("4–5" in i.message or "4-5" in i.message for i in items)


def test_programmatic_checks_flags_repo_order() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="PDF 前端",
                one_liner="课程 PDF 评审平台。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="评论与回复",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="pdf-ui", detail="ui"),
                    )
                ],
            ),
            ProjectExperienceDraft(
                project_name="社交后端",
                one_liner="去中心化内容分发服务。",
                rank=1,
                bullets=[
                    BulletDraft(
                        label="Inbox 收发",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="social-backend", detail="inbox"),
                    )
                ],
            ),
        ]
    )
    items = programmatic_checks(draft, expected_repo_order=["social-backend", "pdf-ui"])
    assert any(i.category == "ats" for i in items)


def test_programmatic_checks_flags_pipeline_overclaim() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="简历 CLI",
                one_liner="本地仓库分析生成 Markdown 简历。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="仓库分析流水线",
                        body=(
                            "独立开发 CLI agent，实现 git 仓库分析到简历生成的完整 pipeline；"
                            "集成检索与职位匹配，"
                            "覆盖从本地代码库提取技术栈到落盘 Markdown 的全链路。"
                        ),
                        evidence=EvidenceRef(source="resume-cli", detail="pipeline"),
                    ),
                    BulletDraft(
                        label="测试与 CI",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="resume-cli", detail="ci"),
                    ),
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any("整站" in i.message or "模块" in i.message for i in items)


def test_programmatic_checks_flags_duty_one_liner() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="天气",
                one_liner="负责实现了天气预报系统的训练流程。",
                rank=0,
                bullets=[
                    BulletDraft(
                        label="模型训练评估",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="hko", detail="train"),
                    ),
                    BulletDraft(
                        label="数据预处理",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="hko", detail="prep"),
                    ),
                ],
            )
        ]
    )
    items = programmatic_checks(draft)
    assert any("one_liner" in i.message for i in items)


def test_programmatic_checks_one_dense_ok_without_richness() -> None:
    draft = ResumeDraft(
        projects=[
            ProjectExperienceDraft(
                project_name="检索层",
                one_liner="混合检索项目素材。",
                rank=1,
                bullets=[
                    BulletDraft(
                        label="Hybrid 召回",
                        body=_DENSE_INBOX,
                        evidence=EvidenceRef(source="resume-cli", detail="hybrid"),
                    )
                ],
            )
        ]
    )
    assert programmatic_checks(draft) == []


def test_format_generate_resume_status_incomplete() -> None:
    incomplete = CritiqueReport(
        must_fix=[
            CritiqueItem(
                severity="must",
                category="structure",
                target="全稿",
                message="审稿未完成，按 write_experience 硬性规则自检修订",
            )
        ],
        critic_complete=False,
    )
    text = format_generate_resume_status([incomplete])
    assert "未完成审稿" in text
    assert "已通过" not in text
    passed = CritiqueReport(must_fix=[], critic_complete=True)
    assert format_generate_resume_status([passed]) == "Critic 已通过"


def test_max_rounds_rejects_zero() -> None:
    with pytest.raises(ValueError):
        writer_critic_loop(
            jd_text="x",
            materials=[],
            profile=SkillProfile(primary_direction="x"),
            llm=MagicMock(),
            max_rounds=0,
        )


def test_writer_critic_loop_revises_when_critic_times_out() -> None:
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
                        "body": (
                            "实现核心读写接口与本地缓存失效策略；补充超时重试、"
                            "结构化日志与关键指标导出，提升故障可观测性；"
                            "对下游错误做降级处理，避免拖垮主请求链路，并保留排障上下文。"
                        ),
                        "evidence": {"source": "r", "detail": "t"},
                    }
                ],
            }
        ],
        "materials_used": ["1"],
    }
    revised = {
        **good,
        "projects": [
            {
                **good["projects"][0],
                "project_name": "P2",
            }
        ],
    }
    critique_ok = {"must_fix": [], "should_fix": [], "passed": ["ok"]}
    llm = MagicMock()
    llm.complete.side_effect = [
        MagicMock(content=json.dumps(good, ensure_ascii=False)),
        TimeoutError("LLM 调用超过 90s"),
        TimeoutError("LLM 调用超过 90s"),
        MagicMock(content=json.dumps(revised, ensure_ascii=False)),
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
    assert draft.projects[0].project_name == "P2"
    assert reports[0].critic_complete is False
    assert reports[-1].approved
    assert llm.complete.call_count == 5


def test_writer_critic_loop_keeps_draft_if_revise_times_out() -> None:
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
                        "body": (
                            "实现核心读写接口与本地缓存失效策略；补充超时重试、"
                            "结构化日志与关键指标导出，提升故障可观测性；"
                            "对下游错误做降级处理，避免拖垮主请求链路，并保留排障上下文。"
                        ),
                        "evidence": {"source": "r", "detail": "t"},
                    }
                ],
            }
        ],
        "materials_used": ["1"],
    }
    llm = MagicMock()
    llm.complete.side_effect = [
        MagicMock(content=json.dumps(good, ensure_ascii=False)),
        TimeoutError("critique 1"),
        TimeoutError("critique 2"),
        TimeoutError("revise"),
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
    assert reports[0].critic_complete is False
    assert reports[0].approved is False
