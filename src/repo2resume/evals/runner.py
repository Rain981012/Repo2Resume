"""Phase 5 评测 runner：检索 + 简历程序化断言 + LLM-as-judge + 结果对比。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from repo2resume.config import AppConfig
from repo2resume.evals.retrieval_metrics import evaluate_search_results
from repo2resume.jobs.matcher import JobMatcher
from repo2resume.resume.critic import programmatic_checks
from repo2resume.storage.models import Job, JobSearchPrefs, ResumeDraft, SearchHit, SkillProfile

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 仓库根下的 evals/（与 src 并列）
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASETS_DIR = REPO_ROOT / "evals" / "datasets"
DEFAULT_RESULTS_DIR = REPO_ROOT / "evals" / "results"

SUITE_RETRIEVAL = "retrieval"
SUITE_RESUME_PROG = "resume_prog"
SUITE_RESUME_JUDGE = "resume_judge"
SUITE_RESUME_E2E = "resume_e2e"
SUITE_JOBS_MATCH = "jobs_match"
ALL_SUITES = (
    SUITE_RETRIEVAL,
    SUITE_RESUME_PROG,
    SUITE_RESUME_JUDGE,
    SUITE_RESUME_E2E,
    SUITE_JOBS_MATCH,
)


@dataclass
class SuiteResult:
    name: str
    metrics: dict[str, float] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    backend: str | None = None
    sample_source: str | None = None


def datasets_dir(override: Path | None = None) -> Path:
    return override or DEFAULT_DATASETS_DIR


def results_dir(override: Path | None = None) -> Path:
    path = override or DEFAULT_RESULTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", text.lower()) if t]


def make_lexical_search_fn(chunks: list[dict[str, Any]]):
    """离线 fixture 检索：按 query token 与 chunk 文本/id 重叠打分（不依赖 embedding）。"""

    def search_fn(query: str, top_k: int = 5) -> list[SearchHit]:
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in chunks:
            blob = f"{chunk.get('doc_id', '')} {chunk.get('text', '')}"
            c_tokens = set(_tokenize(blob))
            overlap = len(q_tokens & c_tokens)
            score = overlap / len(q_tokens)
            if score <= 0:
                continue
            scored.append((score, chunk))
        scored.sort(key=lambda x: (-x[0], x[1].get("doc_id", "")))
        hits: list[SearchHit] = []
        for rank, (score, chunk) in enumerate(scored[:top_k], start=1):
            hits.append(
                SearchHit(
                    doc_id=str(chunk["doc_id"]),
                    text=str(chunk.get("text") or ""),
                    score=float(score),
                    rank=rank,
                    source="fixture",
                    metadata={"repo": chunk.get("repo"), "chunk_type": chunk.get("chunk_type")},
                )
            )
        return hits

    return search_fn


class HashTokenEmbedder:
    """确定性假 embedder：按 token md5 散列到固定维，CI 不下载真实模型。"""

    model_id_or_path = "eval-hash-token"
    dimension = 32

    def encode(self, texts: list[str]) -> list[list[float]]:
        dim = self.dimension
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dim
            for tok in re.findall(r"\w+", text.lower()):
                digest = hashlib.md5(tok.encode()).digest()
                idx = int.from_bytes(digest[:4], "little") % dim
                vec[idx] += 1.0
            norm = sum(x * x for x in vec) ** 0.5
            if norm:
                vec = [x / norm for x in vec]
            out.append(vec)
        return out


def make_hybrid_search_fn(chunks: list[dict[str, Any]], *, work_dir: Path):
    """把 fixture chunks 灌进临时 VectorStore，返回真实 hybrid_search 闭包。"""
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.store import VectorStore
    from repo2resume.storage.db import Database
    from repo2resume.storage.models import DocumentChunk

    work_dir.mkdir(parents=True, exist_ok=True)
    db = Database(work_dir / "eval.db")
    store = VectorStore(db, HashTokenEmbedder())
    docs = [
        DocumentChunk(
            doc_id=str(chunk["doc_id"]),
            text=str(chunk.get("text") or ""),
            repo=chunk.get("repo"),
            chunk_type=str(chunk.get("chunk_type") or "summary"),
            metadata={
                "repo": chunk.get("repo"),
                "chunk_type": chunk.get("chunk_type"),
            },
        )
        for chunk in chunks
    ]
    store.upsert(docs)

    def search_fn(query: str, top_k: int = 5) -> list[SearchHit]:
        return hybrid_search(store, query, top_k=top_k)

    return search_fn


def run_retrieval_suite(
    *,
    data_dir: Path | None = None,
    search_backend: Literal["fixture", "hybrid"] = "fixture",
    work_dir: Path | None = None,
) -> SuiteResult:
    ddir = datasets_dir(data_dir)
    golden_path = ddir / "retrieval_golden.json"
    chunks_path = ddir / "retrieval_fixture_chunks.json"
    try:
        queries = _load_json(golden_path)
        chunks = _load_json(chunks_path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(name=SUITE_RETRIEVAL, error=f"load datasets failed: {exc}")

    if search_backend not in ("fixture", "hybrid"):
        return SuiteResult(
            name=SUITE_RETRIEVAL,
            error=f"unknown search_backend: {search_backend}",
        )

    tmp_ctx = None
    try:
        if search_backend == "fixture":
            search_fn = make_lexical_search_fn(chunks)
        else:
            hybrid_dir = work_dir
            if hybrid_dir is None:
                tmp_ctx = tempfile.TemporaryDirectory()
                hybrid_dir = Path(tmp_ctx.name)
            search_fn = make_hybrid_search_fn(chunks, work_dir=hybrid_dir)
        report = evaluate_search_results(queries, search_fn, k_values=(5,))
        sample_source = None
        if queries:
            sample = search_fn(str(queries[0].get("query") or ""), top_k=1)
            if sample:
                sample_source = sample[0].source
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(
            name=SUITE_RETRIEVAL,
            error=f"search backend {search_backend} failed: {exc}",
            backend=search_backend,
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    cases = []
    for i, q in enumerate(queries):
        cases.append(
            {
                "query": q.get("query"),
                "expected_doc_ids": q.get("expected_doc_ids"),
                "recall@5": report["recall_per_query"][5][i],
                "mrr": report["mrr_per_query"][i],
            }
        )
    return SuiteResult(
        name=SUITE_RETRIEVAL,
        metrics={
            "count": float(report["count"]),
            "recall@5": float(report["recall"][5]),
            "mrr": float(report["mrr"]),
        },
        cases=cases,
        backend=search_backend,
        sample_source=sample_source,
    )


def run_resume_programmatic_suite(*, data_dir: Path | None = None) -> SuiteResult:
    ddir = datasets_dir(data_dir)
    path = ddir / "resume_programmatic_golden.json"
    try:
        cases_raw = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(name=SUITE_RESUME_PROG, error=f"load dataset failed: {exc}")

    cases: list[dict[str, Any]] = []
    passed = 0
    for row in cases_raw:
        case_id = row.get("id", "?")
        try:
            draft = ResumeDraft.model_validate(row["draft"])
        except Exception as exc:  # noqa: BLE001
            cases.append({"id": case_id, "ok": False, "error": f"invalid draft: {exc}"})
            continue
        items = programmatic_checks(
            draft,
            richness=row.get("richness"),
            expected_repo_order=row.get("expected_repo_order"),
        )
        must_cats = {i.category for i in items}
        expect_empty = bool(row.get("expect_must_empty", False))
        expect_cats = set(row.get("expect_must_categories") or [])
        if expect_empty:
            ok = len(items) == 0
        else:
            ok = bool(expect_cats) and expect_cats == must_cats
        if ok:
            passed += 1
        cases.append(
            {
                "id": case_id,
                "ok": ok,
                "must_count": len(items),
                "must_categories": sorted(must_cats),
                "messages": [i.message for i in items[:5]],
            }
        )
    total = len(cases_raw) or 1
    return SuiteResult(
        name=SUITE_RESUME_PROG,
        metrics={
            "count": float(len(cases_raw)),
            "pass_rate": passed / total,
            "passed": float(passed),
        },
        cases=cases,
    )


def _extract_json_obj(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in judge response")
    return json.loads(text[start : end + 1])


def run_resume_judge_suite(
    *,
    config: AppConfig | None = None,
    llm: Any | None = None,
    data_dir: Path | None = None,
) -> SuiteResult:
    ddir = datasets_dir(data_dir)
    path = ddir / "resume_judge_golden.json"
    try:
        cases_raw = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(name=SUITE_RESUME_JUDGE, error=f"load dataset failed: {exc}")

    if llm is None:
        if config is None:
            return SuiteResult(name=SUITE_RESUME_JUDGE, error="need config or llm for judge")
        from repo2resume.llm.client import LLMClient

        llm = LLMClient(config)

    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    template = env.get_template("evals/resume_judge.j2")

    cases: list[dict[str, Any]] = []
    totals: list[float] = []
    for row in cases_raw:
        case_id = row.get("id", "?")
        try:
            draft = ResumeDraft.model_validate(row["draft"])
            system = template.render(
                jd_text=row.get("jd_text") or "(empty)",
                draft_json=json.dumps(draft.model_dump(), ensure_ascii=False, indent=2),
                rubric_notes=row.get("rubric_notes") or "(none)",
            )
            result = llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "按 rubric 输出 JSON 评分。"},
                ],
                temperature=0.0,
                use_cache=False,
            )
            scores = _extract_json_obj(result.content or "")
            total = float(scores.get("total") or 0.0)
            totals.append(total)
            cases.append({"id": case_id, "ok": True, "scores": scores})
        except Exception as exc:  # noqa: BLE001
            cases.append({"id": case_id, "ok": False, "error": str(exc)})

    metrics: dict[str, float] = {
        "count": float(len(cases_raw)),
        "scored": float(len(totals)),
    }
    if totals:
        metrics["judge_avg"] = sum(totals) / len(totals)
    return SuiteResult(name=SUITE_RESUME_JUDGE, metrics=metrics, cases=cases)


def _e2e_hits(raw: list[dict[str, Any]]) -> list[SearchHit]:
    return [SearchHit.model_validate(item) for item in raw]


def _e2e_fact_sheet(raw: Any) -> Any:
    if not raw:
        return None
    from repo2resume.storage.models import FactSheet

    return FactSheet.model_validate(raw)


def _e2e_dense_body(repo: str, text: str) -> str:
    snippet = (text or "").replace("\n", " ").strip()
    if len(snippet) < 40:
        snippet = f"{snippet} {repo} 模块实现与校验".strip()
    return (
        f"实现 {repo} 相关模块（{snippet[:80]}）；"
        "补失败重试与操作审计，避免静默失败并便于定位故障；"
        "用分号衔接动作与结果，保持模块级表述、不写整站主导。"
    )


def grounded_draft_from_case(row: dict[str, Any]) -> ResumeDraft:
    """评测用：按 golden 素材生成带 evidence 的合法草稿（不调用 LLM）。"""
    hits = _e2e_hits(list(row.get("materials") or []))
    repo = "unknown"
    text = ""
    if hits:
        repo = str((hits[0].metadata or {}).get("repo") or "unknown")
        text = hits[0].text or ""
    body = _e2e_dense_body(repo, text)
    extra = _e2e_dense_body(repo, "retry audit cache")
    return ResumeDraft(
        locale="zh-CN",
        job_title=row.get("job_title"),
        projects=[
            {
                "project_name": f"{repo} 项目",
                "one_liner": f"{repo} 相关服务。",
                "rank": 0,
                "bullets": [
                    {
                        "label": "核心模块",
                        "body": body,
                        "evidence": {"source": repo, "detail": hits[0].doc_id if hits else repo},
                    },
                    {
                        "label": "失败重试",
                        "body": extra,
                        "evidence": {"source": repo, "detail": "retry"},
                    },
                ],
            }
        ],
    )


def run_resume_e2e_suite(
    *,
    config: AppConfig | None = None,
    llm: Any | None = None,
    data_dir: Path | None = None,
    max_rounds: int = 1,
    use_cache: bool = False,
    run_judge: bool = False,
) -> SuiteResult:
    """固定 JD+素材，真跑 Writer-Critic，再喂程序化断言（可选 judge）。"""
    ddir = datasets_dir(data_dir)
    path = ddir / "resume_e2e_golden.json"
    try:
        cases_raw = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(name=SUITE_RESUME_E2E, error=f"load dataset failed: {exc}")

    if llm is None:
        if config is None:
            return SuiteResult(name=SUITE_RESUME_E2E, error="need config or llm for resume_e2e")
        from repo2resume.llm.client import LLMClient

        llm = LLMClient(config)

    from repo2resume.resume.critic import writer_critic_loop

    judge_env = None
    if run_judge:
        judge_env = Environment(
            loader=FileSystemLoader(str(PROMPTS_DIR)),
            autoescape=select_autoescape(enabled_extensions=()),
        )

    cases: list[dict[str, Any]] = []
    writer_ok = 0
    prog_ok = 0
    approved = 0
    judge_totals: list[float] = []
    for row in cases_raw:
        case_id = str(row.get("id") or "?")
        try:
            profile = SkillProfile.model_validate(row["profile"])
            materials = _e2e_hits(list(row.get("materials") or []))
            fact_sheet = _e2e_fact_sheet(row.get("fact_sheet"))
            draft, reports = writer_critic_loop(
                jd_text=str(row.get("jd_text") or ""),
                materials=materials,
                profile=profile,
                fact_sheet=fact_sheet,
                llm=llm,
                max_rounds=max_rounds,
                job_title=row.get("job_title"),
                use_cache=use_cache,
            )
            writer_ok += 1
            last = reports[-1] if reports else None
            if last is not None and last.approved:
                approved += 1
            items = programmatic_checks(draft)
            prog_pass = len(items) == 0
            if prog_pass:
                prog_ok += 1
            rec: dict[str, Any] = {
                "id": case_id,
                "ok": True,
                "prog_pass": prog_pass,
                "approved": bool(last.approved) if last is not None else False,
                "must_categories": sorted({i.category for i in items}),
                "n_projects": len(draft.projects),
            }
            if run_judge and judge_env is not None:
                template = judge_env.get_template("evals/resume_judge.j2")
                system = template.render(
                    jd_text=row.get("jd_text") or "(empty)",
                    draft_json=json.dumps(draft.model_dump(), ensure_ascii=False, indent=2),
                    rubric_notes=row.get("rubric_notes") or "(e2e live draft)",
                )
                result = llm.complete(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "按 rubric 输出 JSON 评分。"},
                    ],
                    temperature=0.0,
                    use_cache=False,
                )
                scores = _extract_json_obj(result.content or "")
                rec["scores"] = scores
                judge_totals.append(float(scores.get("total") or 0.0))
            cases.append(rec)
        except Exception as exc:  # noqa: BLE001
            cases.append({"id": case_id, "ok": False, "error": str(exc)})

    total = len(cases_raw) or 1
    metrics: dict[str, float] = {
        "count": float(len(cases_raw)),
        "writer_ok_rate": writer_ok / total,
        "prog_pass_rate": prog_ok / total,
        "approved_rate": approved / total,
    }
    if judge_totals:
        metrics["judge_avg"] = sum(judge_totals) / len(judge_totals)
        metrics["scored"] = float(len(judge_totals))
    return SuiteResult(name=SUITE_RESUME_E2E, metrics=metrics, cases=cases)


class _NoopLLM:
    def complete(self, messages, *, model=None, temperature=0.0, **kwargs):
        from repo2resume.llm.client import CompletionResult

        return CompletionResult(
            content="[]", model="eval-noop", input_tokens=1, output_tokens=1, cost_usd=0.0
        )


def run_jobs_match_suite(*, data_dir: Path | None = None) -> SuiteResult:
    ddir = datasets_dir(data_dir)
    path = ddir / "jobs_ranking_golden.json"
    candidate_path = ddir / "jobs_candidate_golden.json"
    try:
        rows = _load_json(path)
    except Exception as exc:  # noqa: BLE001
        return SuiteResult(name=SUITE_JOBS_MATCH, error=f"load dataset failed: {exc}")

    matcher = JobMatcher(HashTokenEmbedder(), _NoopLLM())
    cases: list[dict[str, Any]] = []
    passed = 0
    for row in rows:
        case_id = str(row.get("id") or "?")
        try:
            profile = SkillProfile.model_validate(row["profile"])
            prefs = JobSearchPrefs.model_validate(row.get("prefs") or {})
            jobs = [Job.model_validate(j) for j in row.get("jobs") or []]
            top_k = int(row.get("top_k") or 5)
            scores = matcher.match_all(
                profile,
                jobs,
                top_k=top_k,
                use_llm=False,
                city=prefs.city,
                is_campus=prefs.is_campus,
            )
            got_ids = [s.job_id for s in scores]
            expect = list(row.get("expect_top_ids") or [])
            forbid = set(row.get("forbid_top_ids") or [])
            ok_expect = True if not expect else (got_ids and got_ids[0] in expect)
            ok_forbid = not any(i in forbid for i in got_ids)
            ok = ok_expect and ok_forbid
            if ok:
                passed += 1
            cases.append(
                {
                    "id": case_id,
                    "ok": ok,
                    "got_top_ids": got_ids,
                    "expect_top_ids": expect,
                    "forbid_top_ids": sorted(forbid),
                }
            )
        except Exception as exc:  # noqa: BLE001
            cases.append({"id": case_id, "ok": False, "error": str(exc)})

    total = len(rows) or 1
    metrics = {
        "count": float(len(rows)),
        "pass_rate": passed / total,
        "passed": float(passed),
    }

    if candidate_path.exists():
        try:
            from repo2resume.jobs.search import _classify_url_confidence

            crows = _load_json(candidate_path)
            cpass = 0
            for crow in crows:
                bucket, conf = _classify_url_confidence(str(crow.get("url") or ""))
                ok = (
                    bucket == crow.get("expected_bucket")
                    and conf == crow.get("expected_confidence")
                )
                cpass += 1 if ok else 0
                cases.append(
                    {
                        "id": crow.get("id"),
                        "ok": ok,
                        "got_bucket": bucket,
                        "got_confidence": conf,
                        "expect_bucket": crow.get("expected_bucket"),
                        "expect_confidence": crow.get("expected_confidence"),
                    }
                )
            ctotal = len(crows) or 1
            metrics["candidate_count"] = float(len(crows))
            metrics["candidate_pass_rate"] = cpass / ctotal
        except Exception as exc:  # noqa: BLE001
            cases.append({"id": "candidate_classifier", "ok": False, "error": str(exc)})

    return SuiteResult(
        name=SUITE_JOBS_MATCH,
        metrics=metrics,
        cases=cases,
    )


def run_all(
    *,
    config: AppConfig | None = None,
    suites: list[str] | None = None,
    use_llm_judge: bool = True,
    llm: Any | None = None,
    data_dir: Path | None = None,
    search_backend: Literal["fixture", "hybrid"] = "fixture",
) -> list[SuiteResult]:
    wanted = list(suites or ALL_SUITES)
    results: list[SuiteResult] = []
    if SUITE_RETRIEVAL in wanted:
        results.append(
            run_retrieval_suite(data_dir=data_dir, search_backend=search_backend)
        )
    if SUITE_RESUME_PROG in wanted:
        results.append(run_resume_programmatic_suite(data_dir=data_dir))
    if SUITE_RESUME_JUDGE in wanted:
        if use_llm_judge:
            results.append(run_resume_judge_suite(config=config, llm=llm, data_dir=data_dir))
        else:
            results.append(
                SuiteResult(
                    name=SUITE_RESUME_JUDGE,
                    metrics={},
                    cases=[],
                    error="skipped (--no-llm)",
                )
            )
    if SUITE_RESUME_E2E in wanted:
        if use_llm_judge or llm is not None:
            results.append(
                run_resume_e2e_suite(
                    config=config,
                    llm=llm,
                    data_dir=data_dir,
                    run_judge=bool(use_llm_judge),
                )
            )
        else:
            results.append(
                SuiteResult(
                    name=SUITE_RESUME_E2E,
                    metrics={},
                    cases=[],
                    error="skipped (--no-llm)",
                )
            )
    if SUITE_JOBS_MATCH in wanted:
        results.append(run_jobs_match_suite(data_dir=data_dir))
    return results


def save_run(results: list[SuiteResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "suites": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_run(path: Path) -> list[SuiteResult]:
    raw = _load_json(path)
    suites = raw.get("suites") if isinstance(raw, dict) else raw
    out: list[SuiteResult] = []
    for s in suites or []:
        out.append(
            SuiteResult(
                name=s["name"],
                metrics={k: float(v) for k, v in (s.get("metrics") or {}).items()},
                cases=list(s.get("cases") or []),
                error=s.get("error"),
                backend=s.get("backend"),
                sample_source=s.get("sample_source"),
            )
        )
    return out


def flatten_metrics(results: list[SuiteResult]) -> dict[str, float]:
    flat: dict[str, float] = {}
    for r in results:
        for key, val in r.metrics.items():
            flat[f"{r.name}.{key}"] = float(val)
    return flat


def compare_runs(
    current: list[SuiteResult],
    baseline: list[SuiteResult],
) -> list[tuple[str, float | None, float | None, float | None]]:
    """返回 (metric_name, baseline, current, delta)。"""
    cur = flatten_metrics(current)
    base = flatten_metrics(baseline)
    keys = sorted(set(cur) | set(base))
    rows: list[tuple[str, float | None, float | None, float | None]] = []
    for key in keys:
        b = base.get(key)
        c = cur.get(key)
        delta = None if b is None or c is None else c - b
        rows.append((key, b, c, delta))
    return rows
