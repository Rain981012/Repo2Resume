"""用真实配置与真实网络跑一遍 search_jobs 工具，打印进度与榜单并自检。

会消耗 Tavily credits 与 LLM 调用，仅手动执行。
"""

from __future__ import annotations

import logging
import re
import sys
from unittest.mock import patch

from repo2resume.agent.builtins import make_search_jobs_tool
from repo2resume.config import load_config
from repo2resume.retrieval.embedder import build_embedder
from repo2resume.storage.db import open_db

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    cfg = load_config()
    db = open_db(cfg.db_path)
    prefs = db.load_job_search_prefs()
    print(f"偏好：{prefs}\n" + "=" * 70)

    progress: list[str] = []

    def _tap(msg: str) -> None:
        progress.append(msg)
        print(f"… {msg}", flush=True)

    embedder = build_embedder(cfg.embed_model, device="cpu")
    with patch("repo2resume.agent.progress.emit_progress", _tap):
        tool = make_search_jobs_tool(cfg, db, embedder)
        out = tool.handler(source="auto")

    print("=" * 70)
    print(out)
    print("=" * 70)

    cards = [
        ln for ln in out.splitlines() if ln.strip()[:2] in {f"{i}." for i in range(1, 10)}
    ]
    n_link = out.count("[职位链接](http")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    check("榜单非空", bool(cards), f"{len(cards)} 条")
    check("每条都有链接", n_link == len(cards), f"链接 {n_link} / 卡片 {len(cards)}")
    check(
        "不含示例职位",
        not any(m in out for m in ("mock-", "CloudScale", "ModelMind", "DataFlow")),
    )
    check("字段名为偏好匹配", "偏好匹配：" in out)
    check(
        "薪资行带用户期望",
        (prefs.salary_range or "") in out if prefs and prefs.salary_range else True,
    )
    check(
        "无 SQLite 并发错误",
        not any("InterfaceError" in p or "SystemError" in p for p in progress),
    )
    check(
        "搜索词无斜杠",
        not any(
            "/" in re.sub(r"site:\S+", "", p.split("query=", 1)[1])
            for p in progress
            if "query=" in p
        ),
    )
    titles = [ln.split(". ", 1)[1].strip() for ln in cards if ". " in ln]
    check("榜单无重复岗位", len(set(titles)) == len(titles), f"{titles}")
    check(
        "偏好行不重复工作地",
        not any(
            ln.count("工作地") > 1
            for ln in out.splitlines()
            if ln.strip().startswith("偏好匹配：")
        ),
    )
    official_q = [p for p in progress if "site:" in p]
    check(
        "发出了官网 site: 检索",
        any("jobs.bytedance.com" in p or "zhaopin.meituan.com" in p for p in official_q),
        f"{len(official_q)} 条进度含 site:",
    )
    official_hits = [
        host
        for host in (
            "jobs.bytedance.com",
            "join.qq.com",
            "zhaopin.meituan.com",
            "jobs.bilibili.com",
            "campushr.hikvision.com",
            "campus-talent.alibaba.com",
            "talent.alibaba.com",
        )
        if host in out
    ]
    check(
        "榜单或正文含官网链接",
        bool(official_hits),
        ",".join(official_hits) or "无",
    )

    live = [p for p in progress if "已召回" in p]
    print("\n漏斗：", live[-1] if live else "（无）")

    db.close()
    print("=" * 70)
    print(f"结果：{len(failures)} 项不合格" if failures else "结果：全部合格")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
