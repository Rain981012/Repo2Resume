"""Tests for CrossEncoderReranker (cross-encoder 对照实现).

用 fake CrossEncoder 避免下载 1.2GB 模型，专注测试 rerank 的编排逻辑：
去重、截断、排序、构造 SearchHit。
"""

from __future__ import annotations

from repo2resume.retrieval.rerank import CrossEncoderReranker
from repo2resume.storage.models import SearchHit


def _hit(doc_id: str, text: str, rank: int = 1) -> SearchHit:
    return SearchHit(doc_id=doc_id, text=text, score=0.0, rank=rank, source="hybrid")


class FakeCrossEncoder:
    """假 CrossEncoder：按 (query, doc) 对返回预设分数。

    真正的 CrossEncoder.predict(pairs) 返回 np.ndarray，这里返回 list[float]，
    CrossEncoderReranker 用 float(score) 转换，两种都兼容。
    """

    def __init__(self, score_map: dict[tuple[str, str], float]) -> None:
        self.score_map = score_map
        self.called_pairs: list[tuple[str, str]] = []

    def predict(self, pairs):
        self.called_pairs = list(pairs)
        return [self.score_map.get(pair, 0.0) for pair in pairs]


def _make_reranker_with_fake_model(fake: FakeCrossEncoder) -> CrossEncoderReranker:
    """构造一个 CrossEncoderReranker，把 _model 直接塞成 fake，跳过懒加载。

    传 device="cpu" 是为了让 resolve_device 早返回，不触发 import torch
    （测试环境可能没装 torch）。
    """
    reranker = CrossEncoderReranker(device="cpu")
    reranker._model = fake
    return reranker


def test_cross_encoder_sorts_by_score():
    fake = FakeCrossEncoder(
        {
            ("devops", "Kubernetes and microservices"): 0.9,
            ("devops", "Python backend work"): 0.4,
            ("devops", "Frontend React components"): 0.1,
        }
    )
    reranker = _make_reranker_with_fake_model(fake)
    hits = [
        _hit("doc-A", "Python backend work"),
        _hit("doc-B", "Kubernetes and microservices"),
        _hit("doc-C", "Frontend React components"),
    ]
    result = reranker.rerank("devops", hits, top_k=3)
    assert [h.doc_id for h in result] == ["doc-B", "doc-A", "doc-C"]
    assert result[0].source == "cross_encoder"
    assert result[0].rank == 1
    assert result[1].rank == 2
    assert result[0].score == 0.9


def test_cross_encoder_respects_top_k():
    fake = FakeCrossEncoder({("q", f"doc{i}"): float(i) for i in range(5)})
    reranker = _make_reranker_with_fake_model(fake)
    hits = [_hit(f"doc{i}", f"doc{i}") for i in range(5)]
    result = reranker.rerank("q", hits, top_k=2)
    assert len(result) == 2
    assert [h.doc_id for h in result] == ["doc4", "doc3"]


def test_cross_encoder_empty_hits():
    reranker = CrossEncoderReranker(device="cpu")
    assert reranker.rerank("q", [], top_k=5) == []


def test_cross_encoder_deduplicates():
    fake = FakeCrossEncoder({("q", "dup"): 0.5})
    reranker = _make_reranker_with_fake_model(fake)
    hits = [_hit("dup", "dup"), _hit("dup", "dup"), _hit("other", "other")]
    result = reranker.rerank("q", hits, top_k=5)
    assert [h.doc_id for h in result] == ["dup", "other"]


def test_cross_encoder_predict_called_with_pairs():
    fake = FakeCrossEncoder({("query", "text-A"): 0.8})
    reranker = _make_reranker_with_fake_model(fake)
    hits = [_hit("doc-A", "text-A")]
    reranker.rerank("query", hits, top_k=1)
    assert fake.called_pairs == [("query", "text-A")]
