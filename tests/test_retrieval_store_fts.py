"""FTS5 关键词检索：BM25 排序与中文召回。"""

from __future__ import annotations

from repo2resume.retrieval.store import VectorStore
from repo2resume.storage.db import Database
from repo2resume.storage.models import DocumentChunk


class _FakeEmbedder:
    model_id_or_path = "fake-embed"
    dimension = 8

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * self.dimension for _ in texts]


def _store(data_dir) -> VectorStore:
    db = Database(data_dir / "store.db")
    return VectorStore(db, _FakeEmbedder())


def test_keyword_search_orders_by_relevance(data_dir):
    store = _store(data_dir)
    store.upsert(
        [
            DocumentChunk(
                doc_id="high",
                text="kubernetes kubernetes kubernetes kubernetes kubernetes cluster",
            ),
            DocumentChunk(
                doc_id="once-a",
                text="this service mentions kubernetes once in passing",
            ),
            DocumentChunk(
                doc_id="once-b",
                text="another note that kubernetes exists here",
            ),
        ]
    )

    hits = store.keyword_search("kubernetes", top_k=3)
    assert [h.doc_id for h in hits][0] == "high"
    assert hits[0].score > hits[-1].score
    assert all(h.score > 0 for h in hits)


def test_keyword_search_empty_query(data_dir):
    store = _store(data_dir)
    assert store.keyword_search("   ") == []


def test_keyword_search_matches_chinese(data_dir):
    store = _store(data_dir)
    store.upsert(
        [
            DocumentChunk(
                doc_id="mq",
                text="实现了高并发消息队列的削峰填谷",
            ),
            DocumentChunk(
                doc_id="unrelated",
                text="this document is about frontend css layout",
            ),
        ]
    )
    hits = store.keyword_search("高并发 消息队列", top_k=5)
    assert any(h.doc_id == "mq" for h in hits)


def test_keyword_search_drops_short_tokens(data_dir):
    store = _store(data_dir)
    store.upsert(
        [
            DocumentChunk(doc_id="go", text="go is a language used here"),
        ]
    )
    assert store.keyword_search("go", top_k=5) == []


def test_keyword_search_fills_repo_from_doc_id(data_dir):
    store = _store(data_dir)
    store.upsert(
        [
            DocumentChunk(
                doc_id="repo:Repo2Resume:summary",
                text="CLI agent hybrid retrieval and tool-use loop",
                repo="Repo2Resume",
            )
        ]
    )
    hits = store.keyword_search("hybrid retrieval", top_k=5)
    assert hits
    assert hits[0].metadata.get("repo") == "Repo2Resume"
