"""Rerank A/B: frozen candidates, no cache sharing, FakeCrossEncoder for CI."""

from __future__ import annotations

from pathlib import Path

from repo2resume.evals.rerank_bench import run_rerank_ab, write_rerank_ab
from repo2resume.retrieval.rerank import CrossEncoderReranker


class _FakeCE:
    def predict(self, pairs):
        return [float(len(doc)) for _q, doc in pairs]


def test_rerank_ab_freezes_same_candidates_for_all_variants(tmp_path: Path) -> None:
    ce = CrossEncoderReranker(device="cpu")
    ce._model = _FakeCE()
    payload = run_rerank_ab(work_dir=tmp_path / "store", cross_encoder=ce)
    frozen = payload["frozen_doc_ids"]
    assert frozen
    assert all(len(ids) <= 20 for ids in frozen)
    assert payload["setup"]["use_cache"] is False
    assert payload["setup"]["candidate_source"] == "hybrid_top20_frozen"
    hybrid = payload["variants"]["hybrid"]
    assert hybrid["recall@5"] >= 0
    assert payload["variants"]["llm_rerank"]["skipped"] is True
    ce_metrics = payload["variants"]["cross_encoder"]
    assert "recall@5" in ce_metrics
    assert ce_metrics["p95_latency_ms"] >= 0


def test_write_rerank_ab(tmp_path: Path) -> None:
    payload = run_rerank_ab(work_dir=tmp_path / "store")
    path = write_rerank_ab(payload, tmp_path / "rerank_ab.json")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "hybrid" in text
    assert "cross_encoder" in text
