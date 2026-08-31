"""Tests for retrieval/hybrid.py."""

from __future__ import annotations

from repo2resume.retrieval.hybrid import reciprocal_rank_fusion
from repo2resume.storage.models import SearchHit


def _hit(doc_id: str, rank: int, source: str = "vector") -> SearchHit:
    return SearchHit(doc_id=doc_id, text=doc_id, score=0.0, rank=rank, source=source)


def test_rrf_basic_ranking():
    """B 在两路都出现，应该排第一。"""
    vector = [_hit("A", 1, "vector"), _hit("B", 2, "vector")]
    keyword = [_hit("B", 1, "keyword"), _hit("C", 2, "keyword")]
    result = reciprocal_rank_fusion(vector, keyword, top_k=10, k=60)
    assert [r.doc_id for r in result] == ["B", "A", "C"]
    assert result[0].source == "hybrid"
    assert result[0].rank == 1
    # B 的分数 = 1/(60+1) + 1/(60+2) > A 的 1/(60+1) > C 的 1/(60+2)
    assert result[0].score > result[1].score > result[2].score


def test_rrf_respects_top_k():
    vector = [_hit("A", 1), _hit("B", 2), _hit("C", 3)]
    keyword = [_hit("D", 1), _hit("E", 2), _hit("F", 3)]
    result = reciprocal_rank_fusion(vector, keyword, top_k=2, k=60)
    assert len(result) == 2


def test_rrf_empty_inputs():
    assert reciprocal_rank_fusion([], []) == []
    single = reciprocal_rank_fusion([_hit("A", 1)], [])
    assert [h.doc_id for h in single] == ["A"]  # 单行也能跑通


def test_rrf_tie_breaker_stable():
    """分数相同时保持原始输入中的相对顺序（Python 排序稳定）。"""
    vector = [_hit("A", 1), _hit("B", 2)]
    keyword = [_hit("C", 1), _hit("D", 2)]
    result = reciprocal_rank_fusion(vector, keyword, top_k=10, k=60)
    # A 和 C 都是 1/61，B 和 D 都是 1/62；稳定排序下 A 在前，C 随后，B 在前，D 随后
    ids = [r.doc_id for r in result]
    assert ids.index("A") < ids.index("C")
    assert ids.index("B") < ids.index("D")


def test_hybrid_search_calls_store():
    """mock VectorStore 验证 hybrid_search 调用两路检索并融合。"""
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.store import VectorStore

    class FakeStore(VectorStore):
        def __init__(self):
            pass

        def vector_search(self, query, top_k=20):
            return [_hit("A", 1, "vector"), _hit("B", 2, "vector")]

        def keyword_search(self, query, top_k=20):
            return [_hit("B", 1, "keyword"), _hit("C", 2, "keyword")]

    store = FakeStore()
    hits = hybrid_search(store, "backend", top_k=10)
    assert [h.doc_id for h in hits] == ["B", "A", "C"]


def test_rrf_preserves_metadata():
    v = [
        SearchHit(
            doc_id="A",
            text="t",
            score=0.9,
            rank=1,
            source="vector",
            metadata={"repo": "Repo2Resume"},
        )
    ]
    kw = [
        SearchHit(
            doc_id="A",
            text="t",
            score=2.0,
            rank=1,
            source="keyword",
            metadata={},
        )
    ]
    out = reciprocal_rank_fusion(v, kw, top_k=5)
    assert out[0].metadata["repo"] == "Repo2Resume"
