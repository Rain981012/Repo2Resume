"""Tests for retrieval/rerank.py."""

from __future__ import annotations

from repo2resume.llm.client import CompletionResult
from repo2resume.retrieval.rerank import LLMReranker, _parse_scores
from repo2resume.storage.models import SearchHit


def _hit(doc_id: str, text: str, rank: int = 1) -> SearchHit:
    return SearchHit(doc_id=doc_id, text=text, score=0.0, rank=rank, source="hybrid")


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, messages, *, model=None, temperature=0.0):
        return CompletionResult(
            content=self.reply,
            model="fake",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0,
        )


def test_llm_reranker_sorts_by_score():
    llm = FakeLLM('{"doc-B": 0.9, "doc-A": 0.4, "doc-C": 0.7}')
    reranker = LLMReranker(llm)
    hits = [
        _hit("doc-A", "Python backend work"),
        _hit("doc-B", "Kubernetes and microservices"),
        _hit("doc-C", "Frontend React components"),
    ]
    result = reranker.rerank("devops backend", hits, top_k=3)
    assert [h.doc_id for h in result] == ["doc-B", "doc-C", "doc-A"]
    assert result[0].source == "rerank"
    assert result[0].rank == 1


def test_llm_reranker_partial_scores_no_keyerror():
    """LLM 漏掉部分 doc_id 时不得 KeyError，缺分按 0。"""
    llm = FakeLLM('{"repo:A:highlight:foo": 0.9}')  # 故意只给一个 id
    reranker = LLMReranker(llm)
    hits = [
        _hit("repo:A:highlight:foo", "litellm glm"),
        _hit("repo:B:summary", "django inbox"),
        _hit("repo:C:readme", "fastapi"),
    ]
    result = reranker.rerank("backend", hits, top_k=3)
    assert result[0].doc_id == "repo:A:highlight:foo"
    assert len(result) == 3


def test_llm_reranker_falls_back_on_invalid_json():
    llm = FakeLLM("not json at all")
    reranker = LLMReranker(llm)
    hits = [_hit("doc-A", "text"), _hit("doc-B", "text")]
    result = reranker.rerank("query", hits, top_k=2)
    # Fallback: keep original order
    assert [h.doc_id for h in result] == ["doc-A", "doc-B"]


def test_parse_scores_strips_code_fence():
    content = '```json\n{"A": 0.8, "B": 0.2}\n```'
    scores = _parse_scores(content, ["A", "B", "C"])
    assert scores == {"A": 0.8, "B": 0.2}


def test_parse_scores_ignores_invalid_ids():
    content = '{"A": 0.8, "X": 0.9}'
    scores = _parse_scores(content, ["A", "B"])
    assert scores == {"A": 0.8}
