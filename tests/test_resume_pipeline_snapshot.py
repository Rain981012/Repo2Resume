from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from repo2resume.resume.pipeline import (
    _resolve_jd_text,
    apply_resume_rerank,
    normalize_resume_reranker,
)
from repo2resume.storage.db import open_db
from repo2resume.storage.models import JobSnapshot, SearchHit


def test_resolve_jd_text_prefers_latest_snapshot(tmp_path: Path) -> None:
    db = open_db(tmp_path / "snapshot.db")
    db.conn.execute(
        "INSERT INTO jobs(id, title, jd_text, source, url) VALUES (?, ?, ?, ?, ?)",
        (
            "tavily-1",
            "Backend",
            "summary jd",
            "tavily",
            "https://www.liepin.com/job/123.shtml",
        ),
    )
    db.conn.commit()
    db.save_job_snapshot(
        JobSnapshot(
            snapshot_id="snap-a",
            job_id="tavily-1",
            fetched_at="2026-08-25T00:00:00+00:00",
            source="detail_fetch",
            url="https://www.liepin.com/job/123.shtml",
            content="full jd from snapshot",
            content_hash="h",
            completeness=0.95,
            verification_status="live",
        )
    )
    text, job_id, title = _resolve_jd_text(db, job_id="tavily-1")
    assert text == "full jd from snapshot"
    assert job_id == "tavily-1"
    assert title == "Backend"
    db.close()


def _hit(doc_id: str) -> SearchHit:
    return SearchHit(
        doc_id=doc_id,
        text=doc_id,
        score=0.1,
        rank=1,
        source="hybrid",
        metadata={"repo": doc_id},
    )


def test_normalize_resume_reranker() -> None:
    assert normalize_resume_reranker("off") == "off"
    assert normalize_resume_reranker(None) == "off"
    assert normalize_resume_reranker("LLM") == "llm"
    assert normalize_resume_reranker("ce") == "cross_encoder"
    assert normalize_resume_reranker("weird") == "off"


def test_apply_resume_rerank_off_keeps_order() -> None:
    hits = [_hit("a"), _hit("b")]
    out, mode = apply_resume_rerank(hits, "jd", mode="off", llm=MagicMock(), top_k=5)
    assert mode == "off"
    assert [h.doc_id for h in out] == ["a", "b"]


def test_apply_resume_rerank_cross_encoder(monkeypatch) -> None:
    class _Fake:
        def rerank(self, query, hits, *, top_k=5):
            _ = query
            rev = list(reversed(hits))
            return rev[:top_k]

    monkeypatch.setattr(
        "repo2resume.retrieval.rerank.CrossEncoderReranker",
        lambda: _Fake(),
    )
    hits = [_hit("a"), _hit("b"), _hit("c")]
    out, mode = apply_resume_rerank(
        hits, "jd", mode="cross_encoder", llm=MagicMock(), top_k=2
    )
    assert mode == "cross_encoder"
    assert [h.doc_id for h in out] == ["c", "b"]


def test_apply_resume_rerank_falls_back_on_error(monkeypatch) -> None:
    class _Boom:
        def rerank(self, query, hits, *, top_k=5):
            raise RuntimeError("no model")

    monkeypatch.setattr(
        "repo2resume.retrieval.rerank.CrossEncoderReranker",
        lambda: _Boom(),
    )
    hits = [_hit("a"), _hit("b")]
    out, mode = apply_resume_rerank(
        hits, "jd", mode="cross_encoder", llm=MagicMock(), top_k=5
    )
    assert mode == "hybrid_fallback"
    assert [h.doc_id for h in out] == ["a", "b"]
