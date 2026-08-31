"""set_job_prefs 闸门 + search 召回池 / 大厂 opt-in。"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repo2resume.agent.builtins import (
    DEFAULT_BIG_TECH,
    _build_search_queries,
    _format_direction_block,
    _hard_eligible,
    make_search_jobs_tool,
    make_set_job_prefs_tool,
)
from repo2resume.config import AppConfig
from repo2resume.storage.db import open_db
from repo2resume.storage.models import (
    Job,
    JobDirection,
    JobSearchPrefs,
    SkillProfile,
    TechStack,
)


@pytest.fixture()
def db(tmp_path: Path):
    return open_db(tmp_path / "t.db")


def test_job_search_prefs_missing_fields() -> None:
    empty = JobSearchPrefs()
    assert "confirmed_directions" in empty.missing_for_search()
    assert "city_or_remote" in empty.missing_for_search()
    ok = JobSearchPrefs(confirmed_directions=["Python 后端"], city="北京")
    assert ok.missing_for_search() == []
    remote_ok = JobSearchPrefs(confirmed_directions=["后端"], remote=True)
    assert remote_ok.missing_for_search() == []


def test_set_job_prefs_persists(db) -> None:
    tool = make_set_job_prefs_tool(db)
    out = tool.handler(
        confirmed_directions=["Python 后端工程师"],
        city="上海",
        top_n=5,
        include_big_tech=False,
    )
    assert "【已保存】" in out
    prefs = db.load_job_search_prefs()
    assert isinstance(prefs, JobSearchPrefs)
    assert prefs.top_n == 5
    assert prefs.include_big_tech is False
    assert prefs.city == "上海"


def test_set_job_prefs_incomplete_still_saves(db) -> None:
    tool = make_set_job_prefs_tool(db)
    out = tool.handler(confirmed_directions=["后端"])
    assert "仍缺项" in out
    assert "city_or_remote" in out


def test_set_job_prefs_merges_and_accepts_nationwide(db) -> None:
    tool = make_set_job_prefs_tool(db)
    out1 = tool.handler(
        confirmed_directions=["Agent 工程师"],
        salary_range="20-35k",
        city="全国",
    )
    assert "【已保存】" in out1
    assert "job_scout" in out1
    # 只补校招「不限」时不得冲掉 city/salary
    out2 = tool.handler(confirmed_directions=["Agent 工程师"], is_campus=None)
    assert "【已保存】" in out2
    prefs = db.load_job_search_prefs()
    assert isinstance(prefs, JobSearchPrefs)
    assert prefs.city == "全国"
    assert prefs.remote is True
    assert prefs.salary_range == "20-35k"


def test_set_job_prefs_rejects_region_alias(db) -> None:
    tool = make_set_job_prefs_tool(db)
    out = tool.handler(
        confirmed_directions=["Python 后端工程师"],
        city="北上广深",
    )
    assert "展开" in out or "缩写" in out
    assert db.load_job_search_prefs() is None


def test_search_jobs_rejects_without_prefs(db) -> None:
    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler()
    assert "set_job_prefs" in out
    assert "拒绝" in out or "需先" in out


def test_search_jobs_rejects_region_alias_city(db) -> None:
    db.save_job_search_prefs(
        JobSearchPrefs(
            confirmed_directions=["Python 后端"],
            city="北上广深",
        )
    )
    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler(source="mock")
    assert "缩写" in out or "展开" in out
    assert "【job_scout已完成】" not in out


def test_build_queries_big_tech_opt_in() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端"],
        city="北京",
        include_big_tech=False,
    )
    qs = _build_search_queries(prefs)
    assert len(qs) == 1
    assert "site:" not in qs[0]

    prefs.include_big_tech = True
    qs2 = _build_search_queries(prefs)
    assert any("site:jobs.bytedance.com" in q for q in qs2)
    assert DEFAULT_BIG_TECH[0] == "字节跳动"


def test_build_queries_splits_saved_cities_and_drops_salary() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端工程师", "全栈工程师"],
        city="北京,上海,广州,深圳",
        is_campus=True,
        salary_range="25-35k",
    )
    qs = _build_search_queries(prefs, limit=None)
    assert any("北京" in q for q in qs)
    assert any("深圳" in q for q in qs)
    assert not any("北上广深" in q for q in qs)
    assert not any("25-35k" in q for q in qs)
    capped = _build_search_queries(prefs, limit=4)
    assert len(capped) == 4
    assert len(qs) >= 8


def test_search_jobs_pool_size(db, monkeypatch) -> None:
    from repo2resume.storage.models import MatchScore

    db.save_job_search_prefs(
        JobSearchPrefs(
            confirmed_directions=["Python 后端"],
            city="北京",
            top_n=5,
        )
    )
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (profile.model_dump_json(),),
    )
    db.conn.commit()

    fake_jobs = [
        Job(
            id=f"j{i}",
            title=f"Engineer {i}",
            company="Co",
            jd_text="Python backend",
            source="mock",
        )
        for i in range(60)
    ]
    captured: dict = {}

    def fake_search(query, count=5, config=None, db=None, source="auto"):
        captured["count"] = count
        return fake_jobs[:count]

    class FakeMatcher:
        def match_all(self, profile, jobs, *, top_k=5, llm_top_n=5, use_llm=True, **kwargs):
            captured["n_candidates"] = len(jobs)
            captured["top_k"] = top_k
            captured["llm_top_n"] = llm_top_n
            captured["use_llm"] = use_llm
            return [
                MatchScore(job_id=j.id, overall_score=0.9, vector_score=0.9, reason="ok")
                for j in jobs[:top_k]
            ]

    monkeypatch.setattr("repo2resume.jobs.search.search_jobs", fake_search)
    monkeypatch.setattr("repo2resume.jobs.matcher.JobMatcher", lambda *a, **k: FakeMatcher())

    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler(source="mock")
    assert "【job_scout已完成】" in out
    assert captured["n_candidates"] == 50
    assert captured["top_k"] >= 5
    assert captured["use_llm"] is True
    assert captured["llm_top_n"] == 10
    assert captured["count"] >= 8
    assert "职位编号：j0" in out
    assert out.count("职位编号：") >= 5
    assert "匹配点：" in out
    assert "偏好匹配：" in out
    assert "薪资：" in out
    assert "缺口：" in out
    assert "综合匹配" in out
    assert "待遇优先" not in out
    assert "技能匹配优先" not in out
    assert "匹配度：" not in out
    assert "按粗排分数排序" not in out


def test_format_direction_block_includes_suggestions() -> None:
    profile = SkillProfile(
        primary_direction="Python 后端",
        secondary_directions=["全栈"],
        job_direction_suggestions=[
            JobDirection(title="Python 工程师", reason="语言占比高"),
            JobDirection(title="后端工程师", reason="Web 依赖"),
        ],
    )
    block = _format_direction_block(profile)
    assert "primary: Python 后端" in block
    assert "secondary: 全栈" in block
    assert "Python 工程师" in block
    assert "请用户确认" in block


def test_search_jobs_refills_until_live_pool(db, monkeypatch) -> None:
    from repo2resume.storage.models import MatchScore

    db.save_job_search_prefs(
        JobSearchPrefs(
            confirmed_directions=["Python 后端"],
            city="北京",
            top_n=5,
        )
    )
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (profile.model_dump_json(),),
    )
    db.conn.commit()

    calls = {"n": 0}

    def fake_search(query, count=5, config=None, db=None, source="auto"):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                Job(
                    id="dead1",
                    title="已下线岗",
                    url="https://www.zhaopin.com/jobdetail/dead.htm",
                    source="tavily",
                    jd_text="Python",
                ),
                Job(
                    id="live1",
                    title="Python 后端",
                    url="https://www.zhaopin.com/jobdetail/live1.htm",
                    source="tavily",
                    jd_text="Python FastAPI 薪资3-4万",
                ),
            ]
        return [
            Job(
                id=f"live{calls['n']}_{i}",
                title=f"Python 后端 {i}",
                url=f"https://www.zhaopin.com/jobdetail/r{calls['n']}{i}.htm",
                source="tavily",
                jd_text="Python FastAPI 薪资3-4万",
            )
            for i in range(max(count, 5))
        ]

    def fake_keep(jobs, **kwargs):
        live = [j for j in jobs if not str(j.id).startswith("dead")]
        return live, len(jobs) - len(live)

    class FakeMatcher:
        def match_all(self, profile, jobs, *, top_k=5, llm_top_n=5, use_llm=True, **kwargs):
            return [
                MatchScore(job_id=j.id, overall_score=0.9, vector_score=0.9, reason="ok")
                for j in jobs[:top_k]
            ]

    monkeypatch.setattr("repo2resume.jobs.search.search_jobs", fake_search)
    monkeypatch.setattr("repo2resume.jobs.liveness.keep_open_jobs", fake_keep)
    monkeypatch.setattr("repo2resume.jobs.matcher.JobMatcher", lambda *a, **k: FakeMatcher())

    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler(source="tavily")
    assert calls["n"] >= 2
    assert out.count("职位编号：") >= 5
    assert "在招" in out
    assert "下线岗已丢弃不计池" in out
    assert "匹配点：" in out


def test_search_jobs_refills_when_matches_are_poor(db, monkeypatch) -> None:
    from repo2resume.storage.models import MatchScore

    db.save_job_search_prefs(
        JobSearchPrefs(
            confirmed_directions=["Python 后端"],
            city="北京",
            top_n=5,
        )
    )
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (profile.model_dump_json(),),
    )
    db.conn.commit()

    calls = {"n": 0}

    def fake_search(query, count=5, config=None, db=None, source="auto"):
        calls["n"] += 1
        prefix = "fpga" if calls["n"] == 1 else "py"
        title = "FPGA 工程师" if calls["n"] == 1 else "Python 后端"
        return [
            Job(
                id=f"{prefix}{calls['n']}_{i}",
                title=f"{title} {i}",
                url=f"https://www.zhaopin.com/jobdetail/{prefix}{calls['n']}{i}.htm",
                source="tavily",
                jd_text=title,
            )
            for i in range(8)
        ]

    class FakeMatcher:
        def match_all(self, profile, jobs, *, top_k=5, llm_top_n=5, use_llm=True, **kwargs):
            out = []
            for j in jobs[:top_k]:
                score = 0.2 if str(j.id).startswith("fpga") else 0.9
                out.append(
                    MatchScore(job_id=j.id, overall_score=score, vector_score=score, reason="x")
                )
            return out

    monkeypatch.setattr("repo2resume.jobs.search.search_jobs", fake_search)
    monkeypatch.setattr(
        "repo2resume.jobs.liveness.keep_open_jobs",
        lambda jobs, **kw: (list(jobs), 0),
    )
    monkeypatch.setattr("repo2resume.jobs.matcher.JobMatcher", lambda *a, **k: FakeMatcher())

    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler(source="tavily")
    assert calls["n"] >= 2
    assert "职位编号：py" in out
    assert out.count("职位编号：") >= 5


def test_search_jobs_keeps_match_order_without_boss_priority(db, monkeypatch) -> None:
    from repo2resume.storage.models import MatchScore

    db.save_job_search_prefs(
        JobSearchPrefs(
            confirmed_directions=["Python 后端"],
            city="北京",
            top_n=2,
        )
    )
    profile = SkillProfile(
        primary_direction="Python 后端",
        tech_stack=TechStack(languages=["Python"]),
    )
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (profile.model_dump_json(),),
    )
    db.conn.commit()

    jobs = [
        Job(
            id="zp1",
            title="智联岗",
            url="https://www.zhaopin.com/jobdetail/CC1.htm",
            source="tavily",
            jd_text="Python 薪资3-4万",
        ),
        Job(
            id="boss1",
            title="Boss岗",
            url="https://www.zhipin.com/job_detail/abc",
            source="tavily",
            jd_text="Python 薪资3-4万",
        ),
    ]

    def fake_search(query, count=5, config=None, db=None, source="auto"):
        return list(jobs)

    class FakeMatcher:
        def match_all(self, profile, jobs, *, top_k=5, llm_top_n=5, use_llm=True, **kwargs):
            # 智联分更高，技能榜应保持匹配分顺序
            by = {"zp1": 0.92, "boss1": 0.90}
            return [
                MatchScore(
                    job_id=j.id,
                    overall_score=by.get(j.id, 0.9),
                    vector_score=0.9,
                    reason="ok",
                )
                for j in jobs[:top_k]
            ]

    monkeypatch.setattr("repo2resume.jobs.search.search_jobs", fake_search)
    monkeypatch.setattr(
        "repo2resume.jobs.liveness.keep_open_jobs",
        lambda jobs, **kw: (list(jobs), 0),
    )
    monkeypatch.setattr("repo2resume.jobs.matcher.JobMatcher", lambda *a, **k: FakeMatcher())

    tool = make_search_jobs_tool(AppConfig(), db, MagicMock())
    out = tool.handler(source="tavily")
    skill = out.split("### Top-")[1]
    assert skill.find("智联岗") < skill.find("Boss岗")
    assert "优先 Boss直聘" not in out


def test_hard_eligible_filters_clear_low_salary_against_expectation() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端"],
        city="上海",
        salary_range="30k 左右",
    )
    low = Job(
        id="low",
        title="后端开发",
        location="上海",
        jd_text="薪资 15-20K，Python 后端开发",
    )
    ok, reason = _hard_eligible(low, prefs)
    assert ok is False
    assert "薪资上限" in reason


def test_hard_eligible_accepts_any_city_in_comma_list() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端"],
        city="北京,上海,广州,深圳",
    )
    shanghai = Job(
        id="sh",
        title="Python 后端",
        location="上海-徐汇",
        jd_text="Python 后端开发 30-40k",
    )
    ok, reason = _hard_eligible(shanghai, prefs)
    assert ok is True, reason

    wuhan = Job(
        id="wh",
        title="Python 后端",
        location="武汉-光谷",
        jd_text="Python 后端开发 30-40k",
    )
    ok_wh, reason_wh = _hard_eligible(wuhan, prefs)
    assert ok_wh is False
    assert "城市不匹配" in reason_wh

    wuhan_title = Job(
        id="wh-title",
        title="python开发工程师招聘_武汉讯锡云科技有限公司招聘",
        location="",
        jd_text="Python FastAPI",
    )
    ok_title, reason_title = _hard_eligible(wuhan_title, prefs)
    assert ok_title is False
    assert "城市不匹配" in reason_title


def test_hard_eligible_filters_senior_years_when_campus() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端"],
        city="上海",
        is_campus=True,
    )
    senior = Job(
        id="veolia",
        title="全栈开发工程师 (Python)",
        location="上海",
        jd_text="任职要求：5-10年开发经验。",
    )
    ok, reason = _hard_eligible(senior, prefs)
    assert ok is False
    assert "校招" in reason

    # 池子太小时可放宽校招硬门，靠综合分把社招岗压下去
    ok_relaxed, _ = _hard_eligible(senior, prefs, relax_campus=True)
    assert ok_relaxed is True

    # 摘要过短不再硬删（会误伤信息少的真岗），改由 _snippet_pref_score 扣分
    thin = Job(
        id="thin",
        title="Python 后端开发工程师招聘",
        location="上海",
        jd_text="",
    )
    ok_thin, reason_thin = _hard_eligible(thin, prefs)
    assert ok_thin is True
    assert reason_thin == ""

    shell = Job(
        id="shell",
        title="岗位详情| 腾讯校招",
        location="深圳",
        jd_text="",
        url="https://join.qq.com/post_detail.html?pid=2&id=176&tid=2",
        source="tavily",
    )
    ok_shell, why_shell = _hard_eligible(shell, prefs)
    assert ok_shell is False
    assert "标题" in why_shell


def test_hard_eligible_requires_http_link() -> None:
    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="上海")
    jd = "负责 Python 后端服务开发，参与接口设计与性能优化，熟悉 FastAPI 与 PostgreSQL。" * 2

    no_url = Job(id="j1", title="Python 后端工程师", location="上海", jd_text=jd, source="tavily")
    ok, why = _hard_eligible(no_url, prefs)
    assert ok is False
    assert "链接" in why

    with_url = Job(
        id="j2",
        title="Python 后端工程师",
        location="上海",
        jd_text=jd,
        source="tavily",
        url="https://jobs.51job.com/all/1.html",
    )
    assert _hard_eligible(with_url, prefs)[0] is True


def test_hard_eligible_exempts_offline_mock_demo() -> None:
    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="上海")
    mock_job = Job(
        id="mock-backend-1",
        title="Senior Backend Engineer",
        location="Remote",
        jd_text="Design and build distributed systems with Python and PostgreSQL." * 3,
        source="mock",
    )
    assert _hard_eligible(mock_job, prefs)[0] is True


def test_salary_line_always_shows_user_expectation() -> None:
    from repo2resume.agent.builtins import _salary_line

    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="上海", salary_range="20-30k")
    job = Job(id="j1", title="Python 后端工程师", jd_text="职责描述", source="tavily")

    # LLM 说「候选人期望未写明」不能盖掉已知期望
    line = _salary_line(job, prefs, "摘要含多个薪资（12-25k等），候选人期望未写明")
    assert "20-30k" in line

    assert "20-30k" in _salary_line(job, prefs, "")
    assert "20-30k" in _salary_line(job, prefs, "JD 未写明")

    paid = Job(id="j2", title="后端 25-35K", jd_text="职责", source="tavily")
    assert "20-30k" in _salary_line(paid, prefs, "无关文本")


def test_preference_line_keeps_user_side_from_prefs() -> None:
    from repo2resume.agent.builtins import _preference_line

    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="上海", is_campus=True)
    job = Job(id="j1", title="Python 后端工程师", location="上海", source="tavily")
    line = _preference_line(job, prefs, "城市/学历/校招摘要未写明")
    assert "用户 上海" in line
    assert "用户要校招" in line


def test_split_direction_breaks_slash_and_restores_role_suffix() -> None:
    from repo2resume.agent.builtins import _split_direction

    assert _split_direction("数据/机器学习工程师") == ["数据工程师", "机器学习工程师"]
    assert _split_direction("Python 后端工程师") == ["Python 后端工程师"]
    assert _split_direction("前端、后端开发") == ["前端开发", "后端开发"]


def test_search_queries_drop_slash_and_cap_directions() -> None:
    prefs = JobSearchPrefs(
        confirmed_directions=[
            "Python 后端工程师",
            "全栈工程师",
            "数据/机器学习工程师",
            "AI 应用开发工程师",
            "DevOps 工程师",
        ],
        city="上海",
        is_campus=True,
    )
    queries = _build_search_queries(prefs, limit=None)
    assert queries
    # 方向名里的斜杠必须拆掉；官网 site:host/campus 的路径斜杠保留
    assert all("/" not in re.sub(r"site:\S+", "", q) for q in queries)
    assert not any("DevOps" in q for q in queries)
    assert any("机器学习工程师" in q for q in queries)


def test_dedupe_key_collapses_punctuation_variants() -> None:
    from repo2resume.agent.builtins import _dedupe_key

    a = Job(id="x", title="Python后端开发/系统开发（高薪，急！）", company="某TOP量化基金")
    b = Job(id="y", title="Python后端开发 / 系统开发（高薪，急!）", company="某TOP量化基金")
    c = Job(id="z", title="全栈工程师", company="某TOP量化基金")
    assert _dedupe_key(a) == _dedupe_key(b)
    assert _dedupe_key(a) != _dedupe_key(c)


def test_preference_line_drops_redundant_llm_clauses() -> None:
    from repo2resume.agent.builtins import _preference_line

    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="上海,深圳", is_campus=True)
    job = Job(id="j1", title="Python开发工程师", location="深圳", source="tavily")
    line = _preference_line(
        job,
        prefs,
        "城市深圳符合候选人偏好（上海,深圳）；职级/校招要求经验不限，与候选人校招/应届偏好吻合；"
        "学历要求本科，候选人作为应届可能符合。",
    )
    # JD 已有结构化城市 → 丢掉 LLM 的城市分句；校招结论以 prefs 为准
    assert line.count("深圳") == 2  # 工作地 + 用户偏好
    assert "职级/校招要求" not in line
    # 顺带提到「应届」的学历分句不能被误删
    assert "学历要求本科" in line


def test_preference_line_flags_campus_conflict() -> None:
    from repo2resume.agent.builtins import _preference_line

    prefs = JobSearchPrefs(confirmed_directions=["Python 后端"], city="广州", is_campus=True)
    senior = Job(
        id="j2",
        title="高级后端架构师",
        location="广州",
        jd_text="任职要求：5-10年开发经验。",
        source="tavily",
    )
    assert "与校招偏好冲突" in _preference_line(senior, prefs, "")
