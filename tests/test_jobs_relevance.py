from repo2resume.jobs.relevance import (
    job_city_flag,
    job_matches_query,
    looks_like_region_alias,
    query_role_tokens,
    split_pref_cities,
)
from repo2resume.storage.models import Job


def test_query_role_tokens_drops_city_and_campus():
    assert query_role_tokens("Python 后端工程师 上海 校招") == ["Python", "后端"]


def test_job_matches_query_keeps_backend_drops_fpga():
    q = "Python 后端工程师 上海"
    backend = Job(id="a", title="Python 后端开发", jd_text="FastAPI", source="x")
    fpga = Job(id="b", title="FPGA工程师", jd_text="芯片", source="x")
    ops = Job(id="c", title="美团骑行-资产配置策略", jd_text="运营", source="x")
    assert job_matches_query(backend, q) is True
    assert job_matches_query(fpga, q) is False
    assert job_matches_query(ops, q) is False


def test_split_pref_cities_comma_not_alias():
    assert split_pref_cities("北京,上海,广州,深圳") == ["北京", "上海", "广州", "深圳"]
    assert split_pref_cities("北上广深") == ["北上广深"]
    assert split_pref_cities("全国") == []
    assert looks_like_region_alias("北上广深")
    assert looks_like_region_alias("江浙沪")
    assert not looks_like_region_alias("北京,上海,广州,深圳")
    assert not looks_like_region_alias("全国")


def test_job_city_flag_title_conflict_and_unknown():
    pref = "北京,上海,广州,深圳"
    assert (
        job_city_flag(
            location="",
            title="python开发工程师招聘_武汉讯锡云科技有限公司招聘",
            jd_text="",
            pref_city=pref,
        )
        == "false"
    )
    assert (
        job_city_flag(
            location="上海-徐汇",
            title="Python 后端",
            jd_text="",
            pref_city=pref,
        )
        == "true"
    )
    assert (
        job_city_flag(
            location="",
            title="Python 后端工程师",
            jd_text="负责 FastAPI",
            pref_city=pref,
        )
        == "unknown"
    )
