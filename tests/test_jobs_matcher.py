"""Tests for jobs/matcher.py."""

from __future__ import annotations

import pytest

from repo2resume.jobs.matcher import JobMatcher, _cosine_similarity
from repo2resume.llm.client import CompletionResult
from repo2resume.storage.models import Job, MatchScore, SkillProfile, TechStack


class FakeEmbedder:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self._cache: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        return self.dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        results = []
        for t in texts:
            if t not in self._cache:
                # 用文本哈希做确定性的伪向量
                vec = [float((hash(t) + i) % 100) / 100.0 for i in range(self.dim)]
                norm = sum(x * x for x in vec) ** 0.5
                vec = [x / norm for x in vec] if norm else vec
                self._cache[t] = vec
            results.append(self._cache[t])
        return results


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.last_messages: list = []

    def complete(self, messages, *, model=None, temperature=0.0, **kwargs):
        self.calls += 1
        self.last_messages = messages
        return CompletionResult(
            content=self.reply,
            model="fake",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0,
        )


def test_cosine_similarity_normalized():
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert _cosine_similarity(a, b) == pytest.approx(1.0)


def test_job_matcher_computes_scores():
    embedder = FakeEmbedder()
    llm = FakeLLM('{"score": 0.75, "reason": "匹配 Python 后端经验"}')
    matcher = JobMatcher(embedder, llm)

    profile = SkillProfile(
        primary_direction="Backend Engineer",
        tech_stack=TechStack(
            languages=["Python", "Go"],
            frameworks=["FastAPI"],
            tools_and_infra=["Kubernetes"],
        ),
    )
    job = Job(
        id="job-1",
        title="Backend Engineer",
        jd_text="Python backend microservices",
        skills=["Python", "Kubernetes"],
    )
    score = matcher.match(profile, job)
    assert isinstance(score, MatchScore)
    assert 0.0 <= score.overall_score <= 1.0
    assert score.vector_score is not None
    assert score.llm_score == pytest.approx(0.75)
    assert "Python" in score.reason


def test_match_all_sorts_and_limits():
    embedder = FakeEmbedder()
    # 批量路径期望 JSON 数组；空数组 → 纯向量排序仍返回 top_k
    llm = FakeLLM("[]")
    matcher = JobMatcher(embedder, llm)
    profile = SkillProfile(primary_direction="Backend Engineer")
    jobs = [
        Job(id="j1", title="A", jd_text="x", skills=[]),
        Job(id="j2", title="B", jd_text="y", skills=[]),
        Job(id="j3", title="C", jd_text="z", skills=[]),
    ]
    scores = matcher.match_all(profile, jobs, top_k=2)
    assert len(scores) == 2
    assert scores[0].overall_score >= scores[1].overall_score
    assert llm.calls == 1  # 一次批量，不再 3 次串行


def test_match_all_batch_llm_once():
    embedder = ConstantEmbedder()
    llm = FakeLLM(
        '[{"job_id":"j1","score":0.9,"reason":"很匹配"},'
        '{"job_id":"j2","score":0.2,"reason":"不太匹配"}]'
    )
    matcher = JobMatcher(embedder, llm)
    profile = SkillProfile(
        primary_direction="Backend Engineer",
        tech_stack=TechStack(languages=["Python"]),
    )
    jobs = [
        Job(id="j1", title="Backend", jd_text="Python", skills=["Python"]),
        Job(id="j2", title="Frontend", jd_text="React", skills=["React"]),
        Job(id="j3", title="Other", jd_text="Go", skills=["Go"]),
    ]
    scores = matcher.match_all(profile, jobs, top_k=3, llm_top_n=2)
    assert llm.calls == 1
    by_id = {s.job_id: s for s in scores}
    assert by_id["j1"].llm_score == pytest.approx(0.9)
    assert "匹配" in by_id["j1"].reason
    # j3 不在 LLM top-2 → 无 llm_score
    assert by_id["j3"].llm_score is None


class TimeoutLLM:
    def __init__(self, timeout_s: float | None = None) -> None:
        self.calls = 0
        self.timeout_s: float | None = None
        if timeout_s is not None:
            self._config = type("Cfg", (), {"llm_timeout_s": timeout_s})()

    def complete(self, messages, *, model=None, temperature=0.0, **kwargs):
        self.calls += 1
        self.timeout_s = kwargs.get("timeout_s")
        raise TimeoutError("LLM 调用超时")


def test_match_all_timeout_falls_back_to_vector():
    embedder = FakeEmbedder()
    timeout_llm = TimeoutLLM(timeout_s=45.0)
    profile = SkillProfile(primary_direction="Backend Engineer")
    jobs = [
        Job(id="j1", title="Backend", jd_text="Python FastAPI", skills=["Python"]),
        Job(id="j2", title="Frontend", jd_text="React", skills=["React"]),
        Job(id="j3", title="Other", jd_text="Go", skills=["Go"]),
    ]
    timed = JobMatcher(embedder, timeout_llm).match_all(
        profile, jobs, top_k=3, llm_top_n=10, use_llm=True
    )
    vector_only = JobMatcher(embedder, FakeLLM("[]")).match_all(
        profile, jobs, top_k=3, llm_top_n=0, use_llm=False
    )
    assert [s.job_id for s in timed] == [s.job_id for s in vector_only]
    assert all(s.llm_score is None for s in timed)
    assert timeout_llm.calls == 1
    assert timeout_llm.timeout_s == pytest.approx(45.0)
    assert [s.overall_score for s in timed] == [s.overall_score for s in vector_only]


def test_match_all_timeout_defaults_to_60_without_config():
    llm = TimeoutLLM()
    JobMatcher(FakeEmbedder(), llm).match_all(
        SkillProfile(primary_direction="Backend"),
        [Job(id="j1", title="A", jd_text="x")],
        top_k=1,
        use_llm=True,
    )
    assert llm.timeout_s == pytest.approx(60.0)


class ConstantEmbedder:
    """所有文本同一向量，用来隔离技能重叠 / 站点平局的效果。"""

    dimension = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_skill_overlap_ranks_keyword_job_above_unrelated():
    from repo2resume.jobs.matcher import _job_skill_tokens, _profile_skill_tokens, _skill_overlap

    profile = SkillProfile(
        primary_direction="Backend",
        tech_stack=TechStack(languages=["Python"], tools_and_infra=["Kubernetes"]),
    )
    k8s_job = Job(
        id="k8s",
        title="Platform Engineer",
        jd_text="Operate Kubernetes clusters",
        skills=["Kubernetes"],
        url="https://jobs.51job.com/beijing/1.html",
    )
    other = Job(
        id="fe",
        title="Frontend Engineer",
        jd_text="React SPA",
        skills=["React"],
        url="https://www.zhipin.com/job_detail/fe",
    )
    assert _skill_overlap(_profile_skill_tokens(profile), _job_skill_tokens(k8s_job)) > 0
    assert _skill_overlap(_profile_skill_tokens(profile), _job_skill_tokens(other)) == 0

    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile, [other, k8s_job], top_k=2, use_llm=False
    )
    assert [s.job_id for s in scores] == ["k8s", "fe"]
    assert scores[0].overall_score > scores[1].overall_score


def test_boss_snippet_job_scores_from_content_not_skills_field():
    from repo2resume.jobs.matcher import _job_skill_tokens

    job = Job(
        id="boss-snip",
        title="后端工程师",
        url="https://www.zhipin.com/job_detail/abc",
        jd_text="岗位职责：Python FastAPI 后端开发。登录注册后可以找工作。",
        skills=[],
    )
    toks = _job_skill_tokens(job)
    assert "python" in toks
    assert "fastapi" in toks
    assert "后端开发" in toks or "后端工程师" in toks


def test_tie_keeps_input_order_when_site_neutral():
    profile = SkillProfile(
        primary_direction="Backend",
        tech_stack=TechStack(languages=["Python"]),
    )
    job_51 = Job(
        id="j51",
        title="Python Engineer",
        jd_text="Python backend",
        skills=["Python"],
        url="https://jobs.51job.com/beijing/1.html",
    )
    job_boss = Job(
        id="jboss",
        title="Python Engineer",
        jd_text="Python backend",
        skills=["Python"],
        url="https://www.zhipin.com/job_detail/x",
    )
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile, [job_51, job_boss], top_k=2, use_llm=False
    )
    assert scores[0].overall_score == scores[1].overall_score
    assert [s.job_id for s in scores] == ["j51", "jboss"]
    assert (scores[0].site_score or 0) == (scores[1].site_score or 0)


class SemanticGapEmbedder:
    """画像与普通岗高相似；含 Kubernetes 的岗向量偏低，模拟专有名词漏召回。"""

    dimension = 2

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i, text in enumerate(texts):
            if i == 0:
                out.append([1.0, 0.0])
            elif "Kubernetes" in text:
                out.append([0.48, 0.8773])
            else:
                out.append([0.62, 0.7846])
        return out


def test_skill_overlap_lifts_keyword_job_into_llm_top_n():
    profile = SkillProfile(
        primary_direction="Backend",
        tech_stack=TechStack(languages=["Python"], tools_and_infra=["Kubernetes"]),
    )
    fillers = [
        Job(id=f"g{i}", title="Backend Engineer", jd_text="distributed services", skills=["Go"])
        for i in range(3)
    ]
    k8s_job = Job(
        id="k8s",
        title="Platform Engineer",
        jd_text="Operate Kubernetes clusters",
        skills=["Kubernetes"],
    )
    llm = FakeLLM('[{"job_id":"k8s","score":0.9,"reason":"技能对口"}]')
    JobMatcher(SemanticGapEmbedder(), llm).match_all(
        profile, [*fillers, k8s_job], top_k=4, llm_top_n=1, use_llm=True
    )
    assert llm.calls == 1
    prompt = llm.last_messages[0]["content"]
    assert "Platform Engineer" in prompt
    assert "id: J1" in prompt
    assert "id: g0" not in prompt


def test_higher_match_beats_preferred_site():
    profile = SkillProfile(
        primary_direction="Backend",
        tech_stack=TechStack(languages=["Python"], tools_and_infra=["Kubernetes"]),
    )
    job_51 = Job(
        id="k8s51",
        title="Platform Engineer",
        jd_text="Kubernetes",
        skills=["Kubernetes"],
        url="https://jobs.51job.com/beijing/1.html",
    )
    job_boss = Job(
        id="feboss",
        title="Frontend Engineer",
        jd_text="React SPA",
        skills=["React"],
        url="https://www.zhipin.com/job_detail/fe",
    )
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile, [job_boss, job_51], top_k=2, use_llm=False
    )
    assert scores[0].job_id == "k8s51"
    assert scores[0].overall_score > scores[1].overall_score


def test_fallback_reason_is_readable_not_internal():
    profile = SkillProfile(
        primary_direction="Backend",
        tech_stack=TechStack(languages=["Python"]),
    )
    job = Job(id="j1", title="Python Engineer", jd_text="Python backend", skills=["Python"])
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile, [job], top_k=1, use_llm=False
    )
    assert "LLM 未得分" not in scores[0].reason
    assert "粗排" not in scores[0].reason
    assert "Python Engineer" in scores[0].reason or "Python" in scores[0].reason


def test_batch_scores_map_j_aliases_and_fill_gaps():
    class ScriptLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.last_messages = []

        def complete(self, messages, *, model=None, temperature=0.0, **kwargs):
            from repo2resume.llm.client import CompletionResult

            self.calls += 1
            self.last_messages = messages
            if self.calls == 1:
                body = '[{"job_id":"J1","score":0.9,"reason":"方向对口，建议投。"}]'
            else:
                body = (
                    '[{"job_id":"J1","score":0.3,"reason":'
                    '"业务偏前端，和后端画像错配，不建议投。"}]'
                )
            return CompletionResult(
                content=body, model="fake", input_tokens=1, output_tokens=1, cost_usd=0.0
            )

    llm = ScriptLLM()
    profile = SkillProfile(primary_direction="Backend")
    jobs = [
        Job(id="tavily-aaa111", title="Backend", jd_text="Python API", skills=["Python"]),
        Job(id="tavily-bbb222", title="CAD", jd_text="C# SolidWorks", skills=["C#"]),
    ]
    scores = JobMatcher(ConstantEmbedder(), llm).match_all(
        profile, jobs, top_k=2, llm_top_n=2, use_llm=True
    )
    assert llm.calls == 2
    by_id = {s.job_id: s for s in scores}
    assert by_id["tavily-aaa111"].llm_score == pytest.approx(0.9)
    assert by_id["tavily-bbb222"].llm_score == pytest.approx(0.3)
    assert "错配" in by_id["tavily-bbb222"].reason or "前端" in by_id["tavily-bbb222"].reason


def test_match_all_hard_filters_parttime_and_city_mismatch():
    profile = SkillProfile(primary_direction="Backend", tech_stack=TechStack(languages=["Python"]))
    jobs = [
        Job(
            id="part",
            title="兼职后端工程师",
            jd_text="part-time backend support",
            location="北京",
            skills=["Python"],
        ),
        Job(
            id="wrong-city",
            title="后端工程师",
            jd_text="full-time backend",
            location="上海",
            skills=["Python"],
        ),
        Job(
            id="ok",
            title="后端工程师",
            jd_text="full-time backend python",
            location="北京",
            skills=["Python"],
        ),
    ]
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile, jobs, top_k=3, use_llm=False, city="北京", is_campus=None
    )
    assert [s.job_id for s in scores] == ["ok"]


def test_match_all_accepts_any_city_in_comma_pref():
    profile = SkillProfile(primary_direction="Backend", tech_stack=TechStack(languages=["Python"]))
    jobs = [
        Job(
            id="sh",
            title="后端工程师",
            jd_text="full-time python",
            location="上海",
            skills=["Python"],
        ),
        Job(
            id="wh",
            title="python开发工程师招聘_武汉讯锡云",
            jd_text="full-time python",
            skills=["Python"],
        ),
    ]
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile,
        jobs,
        top_k=3,
        use_llm=False,
        city="北京,上海,广州,深圳",
        is_campus=None,
    )
    assert [s.job_id for s in scores] == ["sh"]


def test_rich_jd_ranks_above_empty_title_job():
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    empty = Job(
        id="empty",
        title="Python 后端开发工程师招聘",
        jd_text="",
        skills=["Python"],
    )
    rich = Job(
        id="rich",
        title="Python 后端开发工程师招聘",
        location="上海",
        jd_text=(
            "工作地点上海。薪资 25-35k。负责 Python FastAPI 后端与 PostgreSQL。"
            "岗位职责：" + ("维护服务与接口。" * 20)
        ),
        skills=["Python"],
    )
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile,
        [empty, rich],
        top_k=2,
        use_llm=False,
        city="上海",
        salary_range="25-35k",
    )
    assert scores[0].job_id == "rich"
    assert scores[0].overall_score > scores[1].overall_score


def test_company_label_from_title_suffix():
    from repo2resume.agent.builtins import _company_label

    job = Job(
        id="rt",
        title="python开发招聘_软通动力信息技术(集团)股份有限公司招聘",
        skills=["Python"],
    )
    assert "软通动力" in _company_label(job)


def test_company_label_from_official_career_url():
    from repo2resume.agent.builtins import _company_label

    job = Job(
        id="bt",
        title="后端开发工程师-火山引擎",
        url="https://jobs.bytedance.com/campus/position/12/detail",
    )
    assert _company_label(job) == "字节跳动"


def test_campus_pref_ranks_explicit_campus_above_unknown():
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    body = (
        "工作地点上海。负责 Python FastAPI 后端开发与接口维护。"
        "参与服务设计、联调与上线，编写单元测试与文档。"
        "需要独立完成需求拆解，并与产品、测试协作交付。"
    )
    unknown = Job(
        id="unk",
        title="Python 后端工程师",
        location="上海",
        jd_text=body,
        skills=["Python"],
    )
    campus = Job(
        id="camp",
        title="Python 后端工程师校招",
        location="上海",
        jd_text=body,
        skills=["Python"],
    )
    scores = JobMatcher(ConstantEmbedder(), FakeLLM("[]")).match_all(
        profile,
        [unknown, campus],
        top_k=2,
        use_llm=False,
        city="上海",
        is_campus=True,
    )
    assert scores[0].job_id == "camp"
    assert scores[0].overall_score > scores[1].overall_score


def test_campus_unspecified_does_not_penalize_unknown():
    from repo2resume.jobs.matcher import _campus_pref_score

    job = Job(id="x", title="Python 后端", jd_text="Python", skills=["Python"])
    assert _campus_pref_score(job, None) == 1.0
    assert _campus_pref_score(job, True) == 0.32


def test_senior_years_is_campus_false_not_calendar_year():
    from repo2resume.jobs.matcher import _campus_flag, _has_senior_years, _is_job_eligible

    assert _has_senior_years("5-10年经验 Python")
    assert _has_senior_years("3年以上")
    assert not _has_senior_years("2023年校招 Python")
    assert not _has_senior_years("1-3年亦可")
    senior = Job(
        id="veolia",
        title="全栈开发工程师 (Python)",
        jd_text="任职要求：5-10年开发经验，Python。",
        skills=["Python"],
        location="上海",
    )
    assert _campus_flag(senior) == "false"
    assert not _is_job_eligible(senior, city="上海", is_campus=True)


def test_official_url_channel_overrides_empty_jd():
    from repo2resume.jobs.matcher import _campus_flag, _is_job_eligible

    campus = Job(
        id="c",
        title="后端开发工程师-火山引擎",
        jd_text="",
        url="https://jobs.bytedance.com/campus/position/12/detail",
    )
    social = Job(
        id="s",
        title="研发效能专家/架构师-平台",
        jd_text="",
        url="https://talent.didiglobal.com/social/p/65432",
    )
    meituan_campus = Job(
        id="m",
        title="全栈开发工程师",
        jd_text="",
        url="https://zhaopin.meituan.com/web/position/detail?jobUnionId=1&highlightType=campus",
    )
    assert _campus_flag(campus) == "true"
    assert _campus_flag(social) == "false"
    assert _campus_flag(meituan_campus) == "true"
    tencent = Job(
        id="t",
        title="后台开发",
        jd_text="",
        url="https://join.qq.com/post/abc",
    )
    assert _campus_flag(tencent) == "true"
    assert _is_job_eligible(campus, city="北京", is_campus=True)
    assert not _is_job_eligible(social, city="北京", is_campus=True)
    assert _is_job_eligible(tencent, city="深圳", is_campus=True)
    assert _is_job_eligible(social, city="北京", is_campus=False)
    assert not _is_job_eligible(campus, city="北京", is_campus=False)
    tencent_social = Job(
        id="ts",
        title="后台开发",
        jd_text="",
        url="https://careers.tencent.com/jobdesc.html?postId=1",
    )
    ant_campus = Job(
        id="ac",
        title="Java 开发",
        jd_text="",
        url="https://talent.antgroup.com/campus/home",
    )
    kuaishou_campus = Job(
        id="kc",
        title="后端开发",
        jd_text="",
        url="https://campus.kuaishou.cn/recruit/campus/e/#/campus/job-info/1",
    )
    assert _campus_flag(tencent_social) == "false"
    assert _campus_flag(ant_campus) == "true"
    assert _campus_flag(kuaishou_campus) == "true"
    assert not _is_job_eligible(tencent_social, city="深圳", is_campus=True)


def test_thin_snippet_penalized_not_dropped():
    """摘要过短硬删会误伤信息少的真岗（还放过了 JD 很长的假岗），改为综合分扣分。"""
    from repo2resume.jobs.matcher import _is_job_eligible, _snippet_pref_score

    thin = Job(
        id="thin",
        title="Python 后端开发工程师招聘",
        jd_text="",
        skills=["Python"],
    )
    rich = Job(
        id="rich",
        title="Python 后端开发工程师",
        jd_text="负责后端服务开发，熟悉 FastAPI、PostgreSQL、Redis，参与接口设计与性能优化。" * 3,
        skills=["Python", "FastAPI"],
    )
    assert _is_job_eligible(thin, city="上海", is_campus=True)
    assert _snippet_pref_score(thin) < _snippet_pref_score(rich)


def test_enforce_campus_false_keeps_senior_job_in_pool():
    from repo2resume.jobs.matcher import _is_job_eligible

    senior = Job(
        id="senior",
        title="全栈开发工程师",
        jd_text="任职要求：5-10年开发经验。",
        skills=["Python"],
    )
    assert not _is_job_eligible(senior, city="上海", is_campus=True)
    assert _is_job_eligible(senior, city="上海", is_campus=True, enforce_campus=False)


def test_prefs_block_states_known_values() -> None:
    from repo2resume.jobs.matcher import _prefs_block

    block = _prefs_block("北京,上海", "20-30k", True)
    assert "20-30k" in block
    assert "北京,上海" in block
    assert "校招" in block
    assert "未指定" not in block


def test_batch_prompt_carries_prefs() -> None:
    from unittest.mock import MagicMock

    from repo2resume.jobs.matcher import _llm_score_jobs_batch, _prefs_block

    llm = MagicMock()
    llm.complete.return_value = MagicMock(content='[{"job_id": "J1", "score": 0.5}]')
    llm.config = None
    job = Job(id="j1", title="Python 后端", jd_text="Python FastAPI", source="tavily")

    _llm_score_jobs_batch(llm, [job], "画像", prefs_text=_prefs_block("上海", "20-30k", True))
    prompt = llm.complete.call_args.kwargs["messages"][0]["content"]
    assert "20-30k" in prompt
    assert "上海" in prompt


def test_missing_salary_does_not_penalize_big_tech() -> None:
    from repo2resume.jobs.matcher import _salary_pref_score

    unknown_board = Job(
        id="a", title="后端开发", jd_text="负责后端服务", url="https://www.liepin.com/job/1"
    )
    unknown_meituan = Job(
        id="b",
        title="后端开发",
        company="美团",
        jd_text="负责后端服务",
        url="https://www.liepin.com/job/2",
    )
    official = Job(
        id="c",
        title="后端开发",
        jd_text="负责后端服务",
        url="https://jobs.bytedance.com/campus/position/12/detail",
    )
    assert _salary_pref_score(unknown_board, "20-30k") == 0.30
    assert _salary_pref_score(unknown_meituan, "20-30k") == 0.30
    assert _salary_pref_score(official, "20-30k") == 1.0
    shell = Job(
        id="d",
        title="岗位详情| 腾讯校招",
        jd_text="",
        url="https://join.qq.com/post_detail.html?pid=2&id=176&tid=2",
    )
    assert _salary_pref_score(shell, "20-30k") == 0.30


def test_campus_senior_title_is_demoted_not_dropped():
    from repo2resume.jobs.matcher import _campus_pref_score, _is_job_eligible

    senior = Job(
        id="s",
        title="资深后端研发工程师-抖音",
        jd_text="",
        url="https://jobs.bytedance.com/campus/position/1/detail",
        location="北京",
    )
    junior = Job(
        id="j",
        title="后端开发工程师-火山引擎",
        jd_text="",
        url="https://jobs.bytedance.com/campus/position/2/detail",
        location="北京",
    )
    assert _is_job_eligible(senior, city="北京", is_campus=True)
    assert _campus_pref_score(senior, True) == 0.25
    assert _campus_pref_score(junior, True) == 1.0


def test_research_title_direction_mismatch_is_discounted():
    from repo2resume.jobs.matcher import _direction_fit_factor

    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    research = Job(id="r", title="达摩院-医疗AI基础大模型技术研究-阿里星")
    backend = Job(id="b", title="后端开发工程师-火山引擎")
    mixed = Job(id="m", title="推荐算法研究工程师")
    assert _direction_fit_factor(profile, research) == 0.70
    assert _direction_fit_factor(profile, backend) == 1.0
    assert _direction_fit_factor(profile, mixed) == 1.0
