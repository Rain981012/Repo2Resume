"""Tests for evals/retrieval_metrics.py."""

from __future__ import annotations

from repo2resume.evals.retrieval_metrics import compute_mrr, compute_recall_at_k
from repo2resume.storage.models import SearchHit


def _hit(doc_id: str) -> SearchHit:
    return SearchHit(doc_id=doc_id, text=doc_id, score=0.0, rank=1, source="hybrid")


def test_recall_at_k_perfect():
    hits = [_hit("A"), _hit("B"), _hit("C")]
    assert compute_recall_at_k(hits, {"A", "B"}, k=3) == 1.0


def test_recall_at_k_partial():
    hits = [_hit("A"), _hit("C"), _hit("D")]
    assert compute_recall_at_k(hits, {"A", "B"}, k=3) == 0.5


def test_recall_at_k_zero_expected():
    assert compute_recall_at_k([], set(), k=5) == 0.0


def test_mrr_first_rank():
    hits = [_hit("A"), _hit("B")]
    assert compute_mrr(hits, {"A"}) == 1.0


def test_mrr_second_rank():
    hits = [_hit("B"), _hit("A")]
    assert compute_mrr(hits, {"A"}) == 0.5


def test_mrr_no_match():
    hits = [_hit("B"), _hit("C")]
    assert compute_mrr(hits, {"A"}) == 0.0
