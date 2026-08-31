"""详情页解析 + 字段回填 + 时效降权。"""

from __future__ import annotations

from repo2resume.agent.builtins import _apply_job_detail, _build_search_queries
from repo2resume.jobs.detail_parse import parse_detail, parse_liepin_detail
from repo2resume.jobs.matcher import _freshness_factor
from repo2resume.jobs.search import _classify_url_confidence
from repo2resume.jobs.site_rank import site_label
from repo2resume.storage.models import Job, JobSearchPrefs

# 真实猎聘详情页的文本骨架（页头一行 + 正文小标题 + 页尾）
LIEPIN_TEXT = (
    "猎聘网 首页 职位 【广州 Python+SQL 招聘】-华钦科技广州招聘信息-猎聘 "
    "12-22k 广州-天河区 5-10年 本科 招1人 90天前更新 投简历 聊一聊 收藏 微信分享扫码 "
    "绩效奖金 上市公司 王女士 已认证 招聘经理 聊一聊 "
    "职位介绍 主要职责： 1. 负责 MI 报告自动化项目的后端开发，主导数据抽取、转换、加载过程。 "
    "2. 复杂 SQL 开发与优化：针对海量业务数据编写高性能 SQL。 "
    "其他信息 语言要求：英语 公司简介 华钦科技集团2005年成立 猎聘温馨提示 猜你喜欢 别的岗位"
)

CAMPUS_TEXT = (
    "猎聘网 【上海 后端开发 招聘】-华为深圳招聘信息-猎聘 "
    "15-30k 深圳-龙岗区 应届 本科 今日更新 学生可投 微信分享扫码 "
    "岗位职责： 负责产品全生命周期的数据管理，参与集团层级的信息架构建设工作。 "
    "任职要求：熟悉 Python 与 SQL。 公司简介 华为技术有限公司"
)


def _job(**kw) -> Job:
    base = {
        "id": "j1",
        "title": "后端开发",
        "source": "tavily",
        "url": "https://www.liepin.com/job/1.shtml",
    }
    return Job(**{**base, **kw})


class TestParse:
    def test_liepin_header_fields(self) -> None:
        d = parse_liepin_detail(LIEPIN_TEXT)
        assert d.company == "华钦科技"
        assert d.location == "广州"
        assert d.salary_text == "12-22k"
        assert d.experience == "5-10年"
        assert d.education == "本科"
        assert d.days_since_update == 90

    def test_jd_body_excludes_page_furniture(self) -> None:
        d = parse_liepin_detail(LIEPIN_TEXT)
        assert d.jd_text is not None
        assert "MI 报告自动化" in d.jd_text
        assert "猎聘温馨提示" not in d.jd_text
        assert "猜你喜欢" not in d.jd_text
        assert "公司简介" not in d.jd_text

    def test_campus_marker_and_today(self) -> None:
        d = parse_liepin_detail(CAMPUS_TEXT)
        assert d.is_campus is True
        assert d.days_since_update == 0
        assert d.experience == "应届"

    def test_headhunter_listing_labelled_not_named(self) -> None:
        """猎头代招页不公开雇主，写「猎头代招」比编个公司名诚实。"""
        d = parse_liepin_detail(
            "【深圳 Python】-猎头顾问深圳招聘信息-猎聘 15-30k 经验不限 今日更新"
        )
        assert d.company == "猎头代招"

    def test_unknown_site_returns_empty(self) -> None:
        d = parse_detail("https://www.zhipin.com/job_detail/abc.html", LIEPIN_TEXT)
        assert d.empty


class TestEnrich:
    def test_fills_company_and_years_into_jd(self) -> None:
        job = _job()
        assert _apply_job_detail(job, LIEPIN_TEXT) is True
        assert job.company == "华钦科技"
        assert job.location == "广州"
        # 年限进 jd_text，_campus_flag 才判得出这是社招
        assert "5-10年" in job.jd_text
        assert job.days_since_update == 90
        assert job.updated_text == "90天前更新"

    def test_does_not_overwrite_existing_company(self) -> None:
        job = _job(company="已知公司")
        _apply_job_detail(job, LIEPIN_TEXT)
        assert job.company == "已知公司"

    def test_keeps_richer_existing_jd(self) -> None:
        job = _job(jd_text="原有正文" * 500)
        _apply_job_detail(job, LIEPIN_TEXT)
        assert job.jd_text.startswith("原有正文")

    def test_non_liepin_html_is_noop(self) -> None:
        job = _job(url="https://www.zhipin.com/job_detail/x.html")
        assert _apply_job_detail(job, LIEPIN_TEXT) is False


class TestFreshness:
    def test_unknown_update_time_is_not_penalised(self) -> None:
        assert _freshness_factor(_job()) == 1.0

    def test_stale_listing_decays(self) -> None:
        fresh = _freshness_factor(_job(days_since_update=0))
        stale = _freshness_factor(_job(days_since_update=90))
        ancient = _freshness_factor(_job(days_since_update=200))
        assert fresh == 1.0
        assert ancient < stale < fresh


class TestCampusSources:
    def test_bigtech_campus_urls_pass_detail_gate(self) -> None:
        for url in (
            "https://jobs.bytedance.com/campus/position/7412/detail",
            "https://careers.tencent.com/jobdesc.html?postId=123",
            "https://campus.meituan.com/detail/456",
            "https://www.shixiseng.com/intern/inn_abc123",
        ):
            bucket, confidence = _classify_url_confidence(url)
            assert (bucket, confidence) == ("job_detail", "high"), url

    def test_campus_sites_have_display_labels(self) -> None:
        assert site_label("https://jobs.bytedance.com/campus/position/1") == "字节校招"
        assert site_label("https://www.shixiseng.com/intern/x") == "实习僧"

    def test_campus_prefs_add_official_site_queries(self) -> None:
        prefs = JobSearchPrefs(confirmed_directions=["后端开发工程师"], is_campus=True, city="深圳")
        queries = _build_search_queries(prefs, limit=None)
        joined = " | ".join(queries)
        assert "site:jobs.bytedance.com/campus" in joined
        assert "site:jobs.bilibili.com/campus" in joined
        assert "site:campus-talent.alibaba.com" in joined
        assert "highlightType=campus" in joined
        assert "didiglobal.com" not in joined
        assert "campus.kuaishou.com" not in joined
        assert "campus.kuaishou.cn" not in joined

    def test_campus_queries_survive_the_initial_cap(self) -> None:
        """多城市 × 多方向很容易把首轮名额占满，校招词必须挤进去而不是排队尾。"""
        prefs = JobSearchPrefs(
            confirmed_directions=["Python 后端工程师", "全栈工程师", "算法工程师"],
            is_campus=True,
            city="北京,上海,广州,深圳",
        )
        first_round = _build_search_queries(prefs)
        blob = " ".join(first_round)
        for domain in (
            "jobs.bytedance.com/campus",
            "join.qq.com",
            "zhaopin.meituan.com",
            "jobs.bilibili.com/campus",
            "campushr.hikvision.com",
            "campus-talent.alibaba.com",
        ):
            assert f"site:{domain}" in blob, domain

    def test_social_prefs_do_not_add_campus_queries(self) -> None:
        prefs = JobSearchPrefs(
            confirmed_directions=["后端开发工程师"], is_campus=False, city="深圳"
        )
        joined = " | ".join(_build_search_queries(prefs, limit=None))
        assert "site:" not in joined
