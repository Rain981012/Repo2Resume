"""搜岗对外契约护栏：CLI 输出 / job_id 解析路径。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from repo2resume.cli import app
from repo2resume.config import load_config
from repo2resume.resume.pipeline import _resolve_jd_text
from repo2resume.storage.db import open_db
from repo2resume.storage.models import Job, MatchScore, SkillProfile

runner = CliRunner()


def test_jobs_cli_output_contract(data_dir: Path, monkeypatch) -> None:
    _ = data_dir
    cfg = load_config()
    db = open_db(cfg.db_path)
    profile = SkillProfile(primary_direction="Python 后端")
    db.conn.execute(
        "INSERT INTO skill_profiles(payload_json) VALUES (?)",
        (profile.model_dump_json(),),
    )
    db.conn.commit()
    db.close()

    monkeypatch.setattr(
        "repo2resume.jobs.search.search_jobs",
        lambda *args, **kwargs: [
            Job(
                id="tavily-abc123",
                title="Python 后端工程师",
                company="ACME",
                jd_text="Python FastAPI",
                source="tavily",
                url="https://www.zhipin.com/job_detail/abc123",
            )
        ],
    )

    class _FakeMatcher:
        def __init__(self, *args, **kwargs):
            pass

        def match_all(self, profile, jobs, *, top_k=5, llm_top_n=5, use_llm=True):
            return [
                MatchScore(
                    job_id=jobs[0].id,
                    overall_score=0.88,
                    vector_score=0.81,
                    llm_score=0.95,
                    reason="契约测试",
                )
            ]

    monkeypatch.setattr("repo2resume.jobs.matcher.JobMatcher", _FakeMatcher)
    monkeypatch.setattr(
        "repo2resume.retrieval.embedder.build_embedder",
        lambda *args, **kwargs: object(),
    )

    output = Path(cfg.data_dir) / "jobs_contract.json"
    result = runner.invoke(
        app,
        [
            "jobs",
            "--query",
            "Python 后端",
            "--count",
            "1",
            "--source",
            "mock",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.stdout + getattr(result, "stderr", "")
    data = json.loads(output.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"query", "jobs"}
    assert isinstance(data["jobs"], list) and len(data["jobs"]) == 1
    assert {"job_id", "overall_score", "vector_score", "llm_score", "reason"} <= set(
        data["jobs"][0].keys()
    )


def test_resolve_jd_text_prefers_job_id_row_when_jd_text_empty(tmp_path: Path) -> None:
    db = open_db(tmp_path / "resume.db")
    db.conn.execute(
        "INSERT INTO jobs(id, title, jd_text, source) VALUES (?, ?, ?, ?)",
        ("tavily-xyz", "后端工程师", "完整JD文本", "tavily"),
    )
    db.conn.commit()

    text, rid, title = _resolve_jd_text(db, jd_text="", job_id="tavily-xyz")
    assert text == "完整JD文本"
    assert rid == "tavily-xyz"
    assert title == "后端工程师"

    text2, rid2, title2 = _resolve_jd_text(db, jd_text="手工JD", job_id="tavily-xyz")
    assert text2 == "手工JD"
    assert rid2 == "tavily-xyz"
    assert title2 == "后端工程师"
    db.close()
