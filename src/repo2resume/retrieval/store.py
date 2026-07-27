"""向量库 + 全文检索的混合索引。

【AI 辅助】模块：按已确认的接口（DocumentChunk / SearchHit）实现。
底层：ChromaDB 负责稠密向量，SQLite FTS5 负责关键词匹配；对外只暴露统一检索方法。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import chromadb
from pydantic import BaseModel

from repo2resume.retrieval.embedder import Embedder
from repo2resume.storage.db import Database
from repo2resume.storage.models import (
    DocumentChunk,
    RepoStatsBundle,
    SearchHit,
    SkillProfile,
)

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION_NAME = "repo2resume"


class VectorStoreConfig(BaseModel):
    """Store 配置。"""

    collection_name: str = DEFAULT_COLLECTION_NAME
    data_dir: Path | None = None


class VectorStore:
    """ChromaDB + SQLite FTS5 双索引。

    索引生命周期：
      1) 初始化时比对 collection 元数据中的 embed_model / dimension；
         不一致则删除重建，避免换模型后维度错乱。
      2) upsert 时同时写入 ChromaDB（向量）和 fts_documents（关键词）。
      3) 检索时分别查 vector / keyword，交给 hybrid.py 融合。
    """

    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        *,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.collection_name = collection_name
        self.data_dir = db.path.parent
        self.chroma_path = str(self.data_dir / "chroma")
        self._client = chromadb.PersistentClient(path=self.chroma_path)
        self._collection = self._ensure_collection()

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    def _ensure_collection(self):
        """获取或重建 collection。"""
        current_model = self.embedder.model_id_or_path
        current_dim = self.embedder.dimension
        existing_names = {c.name for c in self._client.list_collections()}

        if self.collection_name in existing_names:
            collection = self._client.get_collection(self.collection_name)
            meta = collection.metadata or {}
            if meta.get("embed_model") != current_model or meta.get("dimension") != current_dim:
                logger.warning(
                    "Embedding model changed (%s@%s -> %s@%s); rebuilding collection",
                    meta.get("embed_model"),
                    meta.get("dimension"),
                    current_model,
                    current_dim,
                )
                self._client.delete_collection(self.collection_name)
                collection = self._create_collection(current_model, current_dim)
                self._clear_fts()
            self._set_collection_meta(current_model, current_dim)
            return collection

        collection = self._create_collection(current_model, current_dim)
        self._set_collection_meta(current_model, current_dim)
        self._clear_fts()
        return collection

    def _create_collection(self, model: str, dimension: int):
        return self._client.create_collection(
            name=self.collection_name,
            metadata={"embed_model": model, "dimension": dimension},
        )

    def _get_collection_meta(self) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT embed_model, dimension FROM retrieval_collections WHERE name = ?",
            (self.collection_name,),
        ).fetchone()
        if row is None:
            return {}
        return {"embed_model": row["embed_model"], "dimension": row["dimension"]}

    def _set_collection_meta(self, model: str, dimension: int) -> None:
        self.db.conn.execute(
            "INSERT INTO retrieval_collections(name, embed_model, dimension) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET embed_model=excluded.embed_model, "
            "dimension=excluded.dimension",
            (self.collection_name, model, dimension),
        )
        self.db.conn.commit()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert(self, chunks: list[DocumentChunk]) -> None:
        """批量写入/覆盖：向量 + 关键词。"""
        if not chunks:
            return

        # 1) 删除旧关键词索引（虚拟表不支持 INSERT OR REPLACE）
        ids = [c.doc_id for c in chunks]
        self._delete_fts(ids)

        # 2) 计算 embedding
        texts = [c.text for c in chunks]
        embeddings = self.embedder.encode(texts)

        # 3) 写入 ChromaDB
        self._collection.upsert(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=[
                {
                    "repo": c.repo or "",
                    "chunk_type": c.chunk_type,
                    "metadata_json": json.dumps(c.metadata, ensure_ascii=False),
                }
                for c in chunks
            ],
        )

        # 4) 写入 FTS5
        for c in chunks:
            self.db.conn.execute(
                "INSERT INTO fts_documents(doc_id, text) VALUES (?, ?)",
                (c.doc_id, c.text),
            )
        self.db.conn.commit()

    def clear(self) -> None:
        """清空向量与关键词索引。"""
        existing_names = {c.name for c in self._client.list_collections()}
        if self.collection_name in existing_names:
            self._client.delete_collection(self.collection_name)
        self._collection = self._create_collection(
            self.embedder.model_id_or_path, self.embedder.dimension
        )
        self._clear_fts()
        self._set_collection_meta(self.embedder.model_id_or_path, self.embedder.dimension)

    def _delete_fts(self, ids: list[str]) -> None:
        placeholders = ", ".join("?" for _ in ids)
        self.db.conn.execute(
            f"DELETE FROM fts_documents WHERE doc_id IN ({placeholders})",
            ids,
        )
        self.db.conn.commit()

    def _clear_fts(self) -> None:
        self.db.conn.execute("DELETE FROM fts_documents")
        self.db.conn.commit()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def vector_search(self, query: str, top_k: int = 20) -> list[SearchHit]:
        """用 query 的稠密向量查 ChromaDB。"""
        if not query.strip():
            return []
        embedding = self.embedder.encode([query])[0]
        results = self._collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[SearchHit] = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        dists = results.get("distances", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        for rank, (doc_id, text, dist, meta) in enumerate(
            zip(ids, docs, dists, metas, strict=False), start=1
        ):
            score = 1.0 - float(dist)
            hits.append(
                SearchHit(
                    doc_id=doc_id,
                    text=text or "",
                    score=score,
                    rank=rank,
                    source="vector",
                    metadata=json.loads(meta.get("metadata_json", "{}")) if meta else {},
                )
            )
        return hits

    def keyword_search(self, query: str, top_k: int = 20) -> list[SearchHit]:
        """用 SQLite FTS5 做 BM25 关键词检索。"""
        if not query.strip():
            return []
        # 去掉 query 中 FTS5 特殊字符，避免语法错误
        safe_query = _sanitize_fts_query(query)
        rows = self.db.conn.execute(
            "SELECT doc_id, text, rank FROM fts_documents WHERE text MATCH ? "
            "ORDER BY rank DESC LIMIT ?",
            (safe_query, top_k),
        ).fetchall()
        hits: list[SearchHit] = []
        for rank, row in enumerate(rows, start=1):
            hits.append(
                SearchHit(
                    doc_id=row["doc_id"],
                    text=row["text"],
                    score=float(row["rank"]),
                    rank=rank,
                    source="keyword",
                    metadata={},
                )
            )
        return hits

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def upsert_profile_materials(
        self, profile: SkillProfile, stats: RepoStatsBundle
    ) -> int:
        """把技能画像与统计里的项目素材拆成 DocumentChunk 并索引。"""
        chunks: list[DocumentChunk] = []

        # 1) 画像级别：主方向 + 技术栈 + 领域标签
        tech_text = _render_tech_stack(profile.tech_stack)
        if tech_text:
            chunks.append(
                DocumentChunk(
                    doc_id="profile:tech_stack",
                    text=tech_text,
                    chunk_type="tech_stack",
                )
            )

        for one_liner in profile.project_one_liners:
            chunks.append(
                DocumentChunk(
                    doc_id=f"repo:{one_liner.repo}:summary",
                    text=one_liner.summary,
                    repo=one_liner.repo,
                    chunk_type="summary",
                )
            )

        for highlight in profile.highlights_pool:
            chunks.append(
                DocumentChunk(
                    doc_id=f"repo:{highlight.repo}:highlight:{_slug(highlight.claim)}",
                    text=highlight.claim,
                    repo=highlight.repo,
                    chunk_type="highlight",
                    metadata={"evidence": highlight.evidence.model_dump()},
                )
            )

        # 2) 统计级别：README 摘要、依赖、commit 主题
        for repo in stats.repos:
            readme = repo.readme_excerpt
            if readme:
                chunks.append(
                    DocumentChunk(
                        doc_id=f"repo:{repo.name}:readme",
                        text=readme,
                        repo=repo.name,
                        chunk_type="readme",
                    )
                )
            deps = _render_dependencies(repo.dependencies)
            if deps:
                chunks.append(
                    DocumentChunk(
                        doc_id=f"repo:{repo.name}:deps",
                        text=deps,
                        repo=repo.name,
                        chunk_type="dependencies",
                    )
                )
            commits = ", ".join(repo.recent_commit_subjects[:20])
            if commits:
                chunks.append(
                    DocumentChunk(
                        doc_id=f"repo:{repo.name}:commits",
                        text=commits,
                        repo=repo.name,
                        chunk_type="commits",
                    )
                )

        self.upsert(chunks)
        return len(chunks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_fts_query(query: str) -> str:
    """转义 FTS5 查询中的特殊字符，并去掉空词。"""
    tokens = query.replace('"', '""').split()
    return " ".join(f'"{t}"' for t in tokens if t)


def _slug(text: str) -> str:
    """生成短 doc_id 后缀。"""
    return "".join(c if c.isalnum() or c == " " else " " for c in text).strip()[:30]


def _render_tech_stack(tech: Any) -> str:
    """把 TechStack 渲染成可检索文本。"""
    parts = []
    for field in ["languages", "frameworks", "databases", "tools_and_infra", "other"]:
        items = getattr(tech, field, None) or []
        if items:
            parts.append(f"{field}: {', '.join(items)}")
    return "; ".join(parts)


def _render_dependencies(deps: dict[str, list[str]]) -> str:
    """把依赖字典渲染成文本。"""
    if not deps:
        return ""
    parts = []
    for category, items in deps.items():
        if items:
            parts.append(f"{category}: {', '.join(items)}")
    return "; ".join(parts)
