"""Rerank A/B：同一批 hybrid 候选上对比基线 / LLM / cross-encoder。

刻意避开 ab_subagent_compare.py 的两个坑：
  1. 缓存污染：LLM 路径强制 use_cache=False，三变体不共享调用缓存。
  2. 变量不唯一：每个 query 先冻结 hybrid top-20，三变体只重排这份列表。
"""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any

from repo2resume.evals.retrieval_metrics import compute_mrr, compute_recall_at_k
from repo2resume.evals.runner import (
    DEFAULT_DATASETS_DIR,
    DEFAULT_RESULTS_DIR,
    HashTokenEmbedder,
    make_hybrid_search_fn,
    _load_json,
)
from repo2resume.storage.models import SearchHit

CANDIDATE_K = 20
EVAL_K = 5


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return float(ordered[idx])


def _metrics(hits_by_query: list[list[SearchHit]], queries: list[dict[str, Any]]) -> dict[str, float]:
    recalls: list[float] = []
    mrrs: list[float] = []
    for hits, q in zip(hits_by_query, queries, strict=True):
        expected = set(q["expected_doc_ids"])
        recalls.append(compute_recall_at_k(hits, expected, k=EVAL_K))
        mrrs.append(compute_mrr(hits, expected))
    n = len(queries) or 1
    return {
        "count": float(len(queries)),
        "recall@5": sum(recalls) / n,
        "mrr": sum(mrrs) / n,
    }


def freeze_hybrid_candidates(
    queries: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    work_dir: Path,
    candidate_k: int = CANDIDATE_K,
) -> list[list[SearchHit]]:
    search_fn = make_hybrid_search_fn(chunks, work_dir=work_dir)
    return [search_fn(q["query"], top_k=candidate_k) for q in queries]


def _time_rerank(
    rerank,
    query: str,
    hits: list[SearchHit],
    *,
    top_k: int = EVAL_K,
) -> tuple[list[SearchHit], float]:
    t0 = time.perf_counter()
    out = rerank(query, hits, top_k=top_k)
    return out, (time.perf_counter() - t0) * 1000.0


def run_rerank_ab(
    *,
    data_dir: Path | None = None,
    work_dir: Path | None = None,
    llm_reranker: Any | None = None,
    cross_encoder: Any | None = None,
    skip_reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    ddir = data_dir or DEFAULT_DATASETS_DIR
    queries = _load_json(ddir / "retrieval_golden.json")
    chunks = _load_json(ddir / "retrieval_fixture_chunks.json")
    reasons = skip_reasons or {}

    tmp_ctx = None
    if work_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory()
        work_dir = Path(tmp_ctx.name)
    try:
        frozen = freeze_hybrid_candidates(queries, chunks, work_dir=work_dir)
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    variants: dict[str, Any] = {}

    hybrid_hits = [cands[:EVAL_K] for cands in frozen]
    variants["hybrid"] = {
        **_metrics(hybrid_hits, queries),
        "p95_latency_ms": 0.0,
        "note": "identity top-5 of frozen hybrid top-20",
    }

    if llm_reranker is None:
        variants["llm_rerank"] = {
            "skipped": True,
            "reason": reasons.get("llm_rerank", "llm reranker not provided"),
        }
    else:
        ranked: list[list[SearchHit]] = []
        latencies: list[float] = []
        for q, cands in zip(queries, frozen, strict=True):
            hits, ms = _time_rerank(llm_reranker.rerank, q["query"], cands)
            ranked.append(hits)
            latencies.append(ms)
        variants["llm_rerank"] = {
            **_metrics(ranked, queries),
            "p95_latency_ms": _p95(latencies),
            "use_cache": False,
        }

    if cross_encoder is None:
        variants["cross_encoder"] = {
            "skipped": True,
            "reason": reasons.get("cross_encoder", "cross-encoder not provided"),
        }
    else:
        ranked = []
        latencies = []
        t_load0 = time.perf_counter()
        if hasattr(cross_encoder, "_load"):
            cross_encoder._load()
        load_ms = (time.perf_counter() - t_load0) * 1000.0
        for q, cands in zip(queries, frozen, strict=True):
            hits, ms = _time_rerank(cross_encoder.rerank, q["query"], cands)
            ranked.append(hits)
            latencies.append(ms)
        variants["cross_encoder"] = {
            **_metrics(ranked, queries),
            "p95_latency_ms": _p95(latencies),
            "load_ms": load_ms,
            "model": getattr(cross_encoder, "model_id", None),
        }

    return {
        "setup": {
            "candidate_source": "hybrid_top20_frozen",
            "embedder": HashTokenEmbedder.model_id_or_path,
            "eval_k": EVAL_K,
            "candidate_k": CANDIDATE_K,
            "n_queries": len(queries),
            "use_cache": False,
        },
        "frozen_doc_ids": [[h.doc_id for h in cands] for cands in frozen],
        "variants": variants,
    }


def write_rerank_ab(payload: dict[str, Any], path: Path | None = None) -> Path:
    out = path or (DEFAULT_RESULTS_DIR / "rerank_ab.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
