"""招聘站点偏好排序。"""

from __future__ import annotations

from repo2resume.jobs.site_rank import (
    blend_with_site,
    is_boss_url,
    looks_like_job_title,
    site_label,
    site_score,
    sort_jobs_by_site,
)
from repo2resume.storage.models import Job


def test_site_scores_are_neutral_across_major_boards() -> None:
    assert site_score("https://www.zhipin.com/job_detail/abc123.html") == site_score(
        "https://jobs.51job.com/beijing/153892948.html"
    )
    assert site_label("https://www.zhipin.com/job_detail/x") == "Boss直聘"
    assert site_label("https://jobs.51job.com/beijing/1.html") == "前程无忧"
    assert is_boss_url("https://www.zhipin.com/job_detail/x")
    assert not is_boss_url("https://www.zhaopin.com/jobdetail/x.htm")


def test_official_career_outranks_job_board() -> None:
    assert site_score("https://jobs.bytedance.com/campus/position/1/detail") > site_score(
        "https://www.liepin.com/job/123"
    )
    assert site_label("https://talent.didiglobal.com/social/p/1") == "滴滴社招"
    assert site_label("https://jobs.bytedance.com/experienced/position/1") == "字节跳动社招"
    assert site_label("https://careers.tencent.com/search.html") == "腾讯社招"
    assert site_label("https://hr.163.com") == "网易社招"
    assert site_label("https://campus-talent.alibaba.com/campus/position/1") == "阿里校招"
    assert site_label("https://jobs.bytedance.com/campus/position/1/detail") == "字节校招"
    official = blend_with_site(0.70, "https://jobs.bytedance.com/campus/position/1/detail")
    board = blend_with_site(0.70, "https://www.liepin.com/job/123")
    assert official > board
    thin = blend_with_site(
        0.70,
        "https://join.qq.com/post_detail.html?pid=1",
        title="岗位详情| 腾讯校招",
    )
    assert thin == blend_with_site(0.70, "https://www.liepin.com/job/123")
    real = blend_with_site(
        0.70,
        "https://jobs.bytedance.com/campus/position/1/detail",
        title="后端开发工程师-火山引擎",
    )
    assert real > thin
    boss = blend_with_site(0.73, "https://www.zhipin.com/job_detail/x")
    job51 = blend_with_site(0.73, "https://jobs.51job.com/beijing/1.html")
    assert boss == job51


def test_sort_jobs_by_site_stable() -> None:
    jobs = [
        Job(id="a", title="A", url="https://jobs.51job.com/beijing/1.html"),
        Job(id="b", title="B", url="https://www.zhipin.com/job_detail/b"),
        Job(id="c", title="C", url="https://www.liepin.com/job/123"),
    ]
    ordered = sort_jobs_by_site(jobs)
    assert [j.id for j in ordered] == ["a", "b", "c"]


def test_looks_like_job_title_rejects_shells() -> None:
    assert looks_like_job_title("后端开发工程师-火山引擎")
    assert looks_like_job_title("Python 后端工程师校招")
    assert not looks_like_job_title("岗位详情| 腾讯校招")
    assert not looks_like_job_title("职位详情")
    assert not looks_like_job_title("阿里巴巴校园招聘")
    assert not looks_like_job_title("")
