"""Retrieval layer: embedder, vector/keyword store, hybrid search, rerank.

各子模块按需导入，避免在 __init__ 里急切 import embedder（它依赖 torch），
从而让 hybrid/rerank 等纯逻辑模块的测试不需要安装 torch 即可运行。

用法：
    from repo2resume.retrieval.embedder import build_embedder
    from repo2resume.retrieval.hybrid import hybrid_search
    from repo2resume.retrieval.rerank import LLMReranker
    from repo2resume.retrieval.store import VectorStore
"""

from __future__ import annotations
