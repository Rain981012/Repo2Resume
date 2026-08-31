"""离线跑一遍 search_jobs 工具全流程，断言输出契约。

用固定假职位替换联网检索与在招探测，确保结果可复现：
每次跑出的榜单必须只含有链接的在招岗，且不出现示例职位。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from repo2resume.agent.builtins import make_search_jobs_tool
from repo2resume.config import AppConfig
from repo2resume.storage.db import open_db
from repo2resume.storage.models import Job, JobSearchPrefs, SkillProfile, TechStack

PREFS = JobSearchPrefs(
    confirmed_directions=[
        "Python 后端工程师",
        "全栈工程师",
        "数据/机器学习工程师",
        "AI 应用开发工程师",
        "DevOps / 工具链工程师",
    ],
    city="北京,上海,广州,深圳",
    is_campus=True,
    salary_range="20-30k",
    top_n=5,
    include_big_tech=True,
)

_JD_CAMPUS = (
    "面向 2026 届应届毕业生的校招岗位。负责后端服务开发，"
    "熟悉 Python / FastAPI / PostgreSQL，参与接口设计与性能优化，有 Docker 经验优先。"
)
_JD_SENIOR = (
    "任职要求：5-10年开发经验，精通分布式系统设计，"
    "负责核心链路架构演进，熟悉 Kubernetes 与微服务治理，具备大规模系统调优经验。"
)


def _fake_pool() -> list[Job]:
    """覆盖四类：正常校招岗、无链接岗、社招岗、下线岗。"""
    return [
        Job(
            id="ok-1",
            title="Python 后端开发工程师（校招）",
            company="示例科技",
            location="上海",
            jd_text=_JD_CAMPUS,
            skills=["Python", "FastAPI", "PostgreSQL"],
            source="tavily",
            url="https://jobs.51job.com/all/ok1.html",
        ),
        Job(
            id="ok-2",
            title="全栈开发工程师 26届校招 20-30k",
            company="示例网络",
            location="北京",
            jd_text=_JD_CAMPUS + " 前端使用 React 与 TypeScript。",
            skills=["Python", "React", "TypeScript"],
            source="tavily",
            url="https://www.liepin.com/job/ok2.shtml",
        ),
        Job(
            id="nolink-1",
            title="AI 应用开发工程师（校招）",
            company="无链接公司",
            location="深圳",
            jd_text=_JD_CAMPUS,
            skills=["Python", "LLM"],
            source="tavily",
            url=None,
        ),
        Job(
            id="senior-1",
            title="高级后端架构师",
            company="资深岗公司",
            location="广州",
            jd_text=_JD_SENIOR,
            skills=["Python", "Kubernetes"],
            source="tavily",
            url="https://jobs.51job.com/all/senior1.html",
        ),
        Job(
            id="dead-1",
            title="已下线的 Python 岗",
            company="下线公司",
            location="上海",
            jd_text=_JD_CAMPUS,
            skills=["Python"],
            source="tavily",
            url="https://jobs.51job.com/all/dead1.html",
        ),
    ]


def _profile() -> SkillProfile:
    return SkillProfile(
        primary_direction="Python 后端工程师",
        secondary_directions=["全栈工程师"],
        tech_stack=TechStack(
            languages=["Python", "TypeScript"],
            frameworks=["FastAPI", "Django", "React"],
            databases=["PostgreSQL", "Redis"],
            infra=["Docker"],
        ),
        evidence=[],
    )


class _StubEmbedder:
    """避免下载真模型：按技能词重合度给一个稳定的伪向量。"""

    _DIMS = ("python", "fastapi", "react", "kubernetes", "sql")

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            low = (t or "").lower()
            out.append([1.0 if d in low else 0.0 for d in self._DIMS] + [0.5])
        return out


def main() -> int:
    db_path = Path("/tmp/r2r_smoke/smoke.db")
    if db_path.exists():
        db_path.unlink()
    db = open_db(db_path)
    db.save_job_search_prefs(PREFS)
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (_profile().model_dump_json(),),
    )
    db.conn.commit()

    pool = _fake_pool()
    served: list[str] = []

    def fake_search_jobs(query, *, count, config=None, db=None, source="auto"):
        served.append(query)
        # 每组关键词只给一部分，模拟多轮召回
        idx = len(served) % 2
        return pool[idx::2] if idx else pool

    def fake_keep_open(jobs, **kwargs):
        on_drop = kwargs.get("on_drop")
        kept, dead = [], 0
        for j in jobs:
            if j.id == "dead-1":
                dead += 1
                if on_drop:
                    on_drop(j, "offline")
            else:
                kept.append(j)
        return kept, dead

    cfg = AppConfig(tavily_api_key="t", bocha_api_key=None, llm_api_key=None)
    progress: list[str] = []

    with (
        patch("repo2resume.jobs.search.search_jobs", fake_search_jobs),
        patch("repo2resume.agent.builtins.search_jobs", fake_search_jobs, create=True),
        patch("repo2resume.jobs.liveness.keep_open_jobs", fake_keep_open),
        patch("repo2resume.agent.progress.emit_progress", progress.append),
    ):
        tool = make_search_jobs_tool(cfg, db, _StubEmbedder())
        out = tool.handler(source="tavily")

    print("=" * 70)
    print("\n".join(f"… {p}" for p in progress))
    print("=" * 70)
    print(out)
    print("=" * 70)

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    cards = [ln for ln in out.splitlines() if ln.strip().startswith(("1.", "2.", "3.", "4.", "5."))]
    check("榜单非空", bool(cards), f"{len(cards)} 条")
    check("每条都有链接", out.count("[职位链接](http") == len(cards),
          f"链接 {out.count('[职位链接](http')} / 卡片 {len(cards)}")
    check("不含示例职位", "mock-" not in out and "CloudScale" not in out)
    check("无链接岗被过滤", "无链接公司" not in out)
    check("下线岗被过滤", "已下线的 Python 岗" not in out)
    check("薪资行带用户期望", out.count("20-30k") >= len(cards),
          f"出现 {out.count('20-30k')} 次")
    check("放宽进榜的社招岗带警示", "⚠ JD 写明年限/社招" in out)
    check(
        "偏好行不重复工作地",
        not any(
            ln.count("工作地") > 1
            for ln in out.splitlines()
            if ln.strip().startswith("偏好匹配：")
        ),
    )
    check("字段名为偏好匹配", "偏好匹配：" in out and "\n   偏好：" not in out)
    check("含职位编号", "职位编号：" in out)
    check("硬过滤日志提到无链接",
          any("无有效职位链接" in p for p in progress))
    check("搜索词已拆斜杠", all("/" not in q for q in served), f"{served[:3]}")
    check("方向截断为 3", not any("DevOps" in q for q in served))

    print("=" * 70)
    print(f"结果：{len(failures)} 项不合格" if failures else "结果：全部合格")
    db.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
