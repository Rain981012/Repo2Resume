"""Phase 5 evals runner unit tests (offline, no LLM / embedder)."""

from __future__ import annotations

import json
from pathlib import Path

from repo2resume.evals.runner import (
    SuiteResult,
    compare_runs,
    load_run,
    make_lexical_search_fn,
    run_all,
    run_resume_programmatic_suite,
    run_retrieval_suite,
    save_run,
)


def test_lexical_search_ranks_relevant_chunk() -> None:
    chunks = [
        {"doc_id": "repo:a:summary", "text": "Python FastAPI backend"},
        {"doc_id": "repo:b:summary", "text": "React TypeScript frontend"},
    ]
    hits = make_lexical_search_fn(chunks)("Python FastAPI", top_k=5)
    assert hits
    assert hits[0].doc_id == "repo:a:summary"


def test_run_retrieval_and_programmatic_suites() -> None:
    results = run_all(suites=["retrieval", "resume_prog"], use_llm_judge=False)
    by_name = {r.name: r for r in results}
    assert by_name["retrieval"].error is None
    assert by_name["retrieval"].backend == "fixture"
    assert by_name["retrieval"].metrics["recall@5"] > 0.5
    assert by_name["resume_prog"].error is None
    assert by_name["resume_prog"].metrics["pass_rate"] == 1.0


def test_jobs_match_suite_runs() -> None:
    results = run_all(suites=["jobs_match"], use_llm_judge=False)
    assert len(results) == 1
    r = results[0]
    assert r.name == "jobs_match"
    assert r.error is None
    assert r.metrics["count"] >= 1
    assert r.metrics["pass_rate"] >= 0.5


def test_retrieval_hybrid_backend_uses_real_path(tmp_path: Path) -> None:
    fixture = run_retrieval_suite(search_backend="fixture")
    hybrid = run_retrieval_suite(search_backend="hybrid", work_dir=tmp_path / "hybrid-store")
    assert hybrid.error is None, hybrid.error
    assert hybrid.backend == "hybrid"
    assert hybrid.metrics["count"] == fixture.metrics["count"]
    assert hybrid.metrics["recall@5"] > 0
    assert hybrid.sample_source == "hybrid"
    assert fixture.sample_source == "fixture"
    # T8: 负例 chunk 后两路不应再打成同一组分数（当前差异主要在 MRR）
    assert (
        hybrid.metrics["recall@5"] != fixture.metrics["recall@5"]
        or hybrid.metrics["mrr"] != fixture.metrics["mrr"]
    )


def test_judge_skipped_with_no_llm() -> None:
    results = run_all(suites=["resume_judge", "resume_e2e"], use_llm_judge=False)
    assert len(results) == 2
    assert results[0].error and "skipped" in results[0].error
    assert results[1].error and "skipped" in results[1].error


def test_save_load_and_compare(tmp_path: Path) -> None:
    a = [SuiteResult(name="retrieval", metrics={"recall@5": 0.8, "mrr": 0.7})]
    b = [SuiteResult(name="retrieval", metrics={"recall@5": 0.9, "mrr": 0.7})]
    path = tmp_path / "run.json"
    save_run(a, path)
    loaded = load_run(path)
    assert loaded[0].metrics["recall@5"] == 0.8
    rows = compare_runs(b, a)
    by = {name: delta for name, _b, _c, delta in rows}
    assert abs(by["retrieval.recall@5"] - 0.1) < 1e-9
    assert abs(by["retrieval.mrr"] - 0.0) < 1e-9


def test_programmatic_golden_file_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "evals" / "datasets" / "resume_programmatic_golden.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) >= 4
    assert run_resume_programmatic_suite().metrics["pass_rate"] == 1.0
    assert run_retrieval_suite().metrics["count"] >= 10


def test_resume_judge_with_fake_llm() -> None:
    class FakeLLM:
        def complete(self, messages, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                content=json.dumps(
                    {
                        "specificity": 4,
                        "groundedness": 4,
                        "jd_relevance": 4,
                        "no_overclaim": 5,
                        "total": 4.25,
                        "comment": "ok",
                    }
                )
            )

    from repo2resume.evals.runner import run_resume_judge_suite

    result = run_resume_judge_suite(llm=FakeLLM())
    assert result.error is None
    assert result.metrics["judge_avg"] == 4.25
    assert result.metrics["scored"] >= 50
    assert result.metrics["count"] >= 50


def test_resume_judge_golden_has_fifty_polarity_probes() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "evals" / "datasets" / "resume_judge_golden.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 50
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == 50
    polarities = {row.get("polarity") for row in rows}
    assert polarities >= {
        "grounded",
        "overclaim",
        "slogan",
        "mismatch",
        "no_evidence",
        "mixed",
    }
    from repo2resume.storage.models import ResumeDraft

    for row in rows:
        ResumeDraft.model_validate(row["draft"])
        assert row.get("jd_text")
        assert row.get("rubric_notes")


def test_programmatic_equality_catches_extra_category(tmp_path: Path) -> None:
    """T10a：期望只有 fact 时，若同时触发 style，必须判失败（旧 issubset 会放过）。"""
    payload = [
        {
            "id": "expect_fact_only_but_also_short",
            "expect_must_empty": False,
            "expect_must_categories": ["fact"],
            "draft": {
                "locale": "zh-CN",
                "projects": [
                    {
                        "project_name": "订单服务",
                        "one_liner": "订单中台。",
                        "bullets": [
                            {
                                "label": "下单",
                                "body": "负责后端开发",
                                "evidence": {"source": "", "detail": ""},
                            }
                        ],
                    }
                ],
            },
        }
    ]
    (tmp_path / "resume_programmatic_golden.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    result = run_resume_programmatic_suite(data_dir=tmp_path)
    assert result.error is None
    assert result.metrics["pass_rate"] == 0.0
    assert result.cases[0]["ok"] is False


def test_resume_e2e_grounded_playback_passes_prog() -> None:
    from types import SimpleNamespace

    from repo2resume.evals.runner import (
        datasets_dir,
        grounded_draft_from_case,
        run_resume_e2e_suite,
    )

    rows = json.loads((datasets_dir() / "resume_e2e_golden.json").read_text(encoding="utf-8"))

    class Playback:
        def complete(self, messages, **kwargs):
            text = "\n".join(str(m.get("content") or "") for m in messages)
            if "CritiqueReport" in text or "按清单审稿" in text:
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "must_fix": [],
                            "should_fix": [],
                            "passed": ["ok"],
                            "critic_complete": True,
                        }
                    )
                )
            for row in rows:
                jd = str(row.get("jd_text") or "")
                if jd and jd[:24] in text:
                    return SimpleNamespace(content=grounded_draft_from_case(row).model_dump_json())
            return SimpleNamespace(content=grounded_draft_from_case(rows[0]).model_dump_json())

    result = run_resume_e2e_suite(llm=Playback(), run_judge=False)
    assert result.error is None, result.error
    assert result.metrics["count"] >= 5
    assert result.metrics["writer_ok_rate"] == 1.0
    assert result.metrics["prog_pass_rate"] == 1.0


def test_resume_e2e_missing_evidence_drops_prog() -> None:
    """T9 自检：生成稿没有 evidence 时 prog_pass_rate 必须掉下来。"""
    from types import SimpleNamespace

    from repo2resume.evals.runner import run_resume_e2e_suite

    class EmptyEvidence:
        def complete(self, messages, **kwargs):
            text = "\n".join(str(m.get("content") or "") for m in messages)
            if "CritiqueReport" in text or "按清单审稿" in text:
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "must_fix": [],
                            "should_fix": [],
                            "passed": ["ok"],
                            "critic_complete": True,
                        }
                    )
                )
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "locale": "zh-CN",
                        "projects": [
                            {
                                "project_name": "订单服务",
                                "one_liner": "订单中台。",
                                "rank": 0,
                                "bullets": [
                                    {
                                        "label": "下单",
                                        "body": "负责后端开发",
                                        "evidence": {"source": "", "detail": ""},
                                    }
                                ],
                            }
                        ],
                    }
                )
            )

    result = run_resume_e2e_suite(llm=EmptyEvidence(), run_judge=False)
    assert result.error is None, result.error
    assert result.metrics["writer_ok_rate"] == 1.0
    assert result.metrics["prog_pass_rate"] < 1.0


def test_writer_prompt_still_requires_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "src/repo2resume/prompts/resume/write_experience.j2").read_text(encoding="utf-8")
    assert "每条 bullet 必须带 `evidence`" in text
