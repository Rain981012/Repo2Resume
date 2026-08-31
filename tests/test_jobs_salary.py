"""薪资抽取：单位必需，避免把年限/人数区间当成薪资。"""

from __future__ import annotations

import pytest

from repo2resume.jobs.salary import extract_salary_info, salary_mid_k


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("薪资 20-30k", 20.0, 30.0),
        ("月薪 20~30K/月", 20.0, 30.0),
        ("1.5-2万", 15.0, 20.0),
        ("8-12千", 8.0, 12.0),
        ("20000-30000元", 20.0, 30.0),
        ("薪资20到30", 20.0, 30.0),
    ],
)
def test_extract_known_salary(text: str, lo: float, hi: float) -> None:
    info = extract_salary_info(text)
    assert info.known
    assert info.min == pytest.approx(lo)
    assert info.max == pytest.approx(hi)


@pytest.mark.parametrize(
    "text",
    [
        "任职要求：5-10年开发经验。",
        "1-3年相关经验",
        "团队规模 3-5 人",
        "2026-2027 届毕业生",
        "职责描述，无薪资信息",
    ],
)
def test_year_and_headcount_ranges_are_not_salary(text: str) -> None:
    info = extract_salary_info(text)
    assert not info.known
    assert salary_mid_k(info) is None


def test_senior_years_job_not_filtered_by_salary() -> None:
    """回归：`5-10年经验` 曾被读成 5-10K，触发「薪资上限低于目标」误杀。"""
    from repo2resume.agent.builtins import _hard_eligible
    from repo2resume.storage.models import Job, JobSearchPrefs

    prefs = JobSearchPrefs(
        confirmed_directions=["Python 后端"], city="上海", salary_range="20-30k"
    )
    job = Job(
        id="j1",
        title="全栈开发工程师",
        location="上海",
        jd_text="任职要求：5-10年开发经验。",
        source="tavily",
        url="https://jobs.51job.com/all/1.html",
    )
    ok, why = _hard_eligible(job, prefs)
    assert "薪资" not in why
    assert ok is True
