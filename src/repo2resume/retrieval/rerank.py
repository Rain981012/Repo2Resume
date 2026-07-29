"""Rerank：对混合检索召回的候选做精排。

【手写】填空题 — 把 pass 换成真实代码，不要让 AI 整块生成实现体。

================================================================================
这部分是做什么用的？
================================================================================

混合检索负责"找全"（召回尽可能多的相关文档），但前排不一定最相关。
rerank 负责"排准"：用更强的模型（这里是 LLM，生产里也可以是 cross-encoder）
对候选文档逐对/逐列表打分，把真正相关的推到前面。

典型两阶段 RAG 流程：
  检索（大召回、轻量） -> top 10/20 -> rerank（精排、更重） -> top 5 -> 交给 LLM 生成。

================================================================================
为什么重要？
================================================================================

1. 检索模型（双塔 embedding）为了速度牺牲精度，rerank 用交叉注意力弥补。
2. 把相关素材排到前面，能显著减少 LLM 幻觉，因为 LLM 更关注上下文前面的内容。
3. 这是学习"双塔 vs 交叉编码"、"LLM-as-judge"最好的实践落点。

================================================================================
涉及哪些 Agent 开发知识点？
================================================================================

- 两阶段检索：recall + rerank 的分工。
- LLM-as-judge：让 LLM 输出结构化评分，再被程序消费。
- 结构化输出解析：JSON 提取、代码围栏处理、异常降级。
- 成本/延迟 tradeoff：LLM rerank 贵但简单；cross-encoder 便宜但需本地部署。
- 确定性代码兜底概率性模型：解析失败时回到原始顺序，不让 agent 卡住。

================================================================================
面试可能会问什么？
================================================================================

Q1: 双塔模型（bi-encoder）和交叉编码（cross-encoder）有什么区别？
   A: 双塔分别把 query 和 doc 编码成向量，点积计算，速度快，适合大规模召回；
      cross-encoder 把 query+doc 一起输入模型，注意力能交互，精度高，但慢，适合小批量 rerank。

Q2: 为什么 LLM rerank 比 cross-encoder 更贵？
   A: LLM 每次调用都要处理整个候选列表 prompt，输入 token 多；cross-encoder 模型小、本地跑、
      只处理成对文本。

Q3: LLM 返回坏 JSON 怎么办？
   A: 代码里做解析 try-except；去掉 ```json 围栏；只保留有效 doc_id；失败时降级到原始顺序。

Q4: rerank 的 top_k 应该取多少？
   A: 取决于下游 LLM 的上下文预算和成本；通常取 5-10 条，让 LLM 有足够素材又不浪费 token。

Q5: 怎么判断 rerank 有没有提升？
   A: 用 golden dataset 算 MRR 或 NDCG@K；对比 rerank 前后的平均得分。

================================================================================
每填完一个函数跑：pytest tests/test_retrieval_rerank.py -q
================================================================================
"""

from __future__ import annotations

import json  # noqa: F401  # 实现 _parse_scores 时会用到
import logging
from typing import Protocol, runtime_checkable

from repo2resume.llm.client import LLMClient
from repo2resume.retrieval._device import (
    resolve_device,  # noqa: F401  # CrossEncoderReranker.__init__ 会用到
)
from repo2resume.storage.models import SearchHit

logger = logging.getLogger(__name__)


@runtime_checkable
class Reranker(Protocol):
    """统一 rerank 接口：输入候选，输出按相关度排序后的候选。

    为什么用 Protocol？
      - 后面会有 LLMReranker 和 CrossEncoderReranker 两种实现；
      - Protocol 让调用方只关心接口，不关心具体实现，方便切换和单测 mock。
    """

    def rerank(self, query: str, hits: list[SearchHit], *, top_k: int = 5) -> list[SearchHit]: ...


class LLMReranker:
    """用 LLM 对候选文档做零样本精排。"""

    def __init__(self, llm: LLMClient, model: str | None = None) -> None:
        self.llm = llm
        self.model = model

    def rerank(self, query: str, hits: list[SearchHit], *, top_k: int = 5) -> list[SearchHit]:
        """对 hits 按 query 相关度重排，返回 top_k。

        为什么 temperature=0.0？
          - rerank 需要确定性评分，随机性会让同一批候选每次排序不同，评测不可复现。
        """
        if not hits:
            return []
        unique_hits = _deduplicate_by_doc_id(hits)
        candidates = unique_hits[: max(top_k * 2, 10)]
        prompt = _build_rerank_prompt(query, candidates)
        messages = [{"role": "user", "content": prompt}]
        result = self.llm.complete(messages=messages, model=self.model, temperature=0.0)
        scores = _parse_scores(result.content, [h.doc_id for h in candidates])
        if not scores:
            return unique_hits[:top_k]
        # LLM 常漏掉部分 doc_id；缺分视为 0，避免 KeyError 整段失败重试
        sorted_candidates = sorted(
            candidates,
            key=lambda h: scores.get(h.doc_id, 0.0),
            reverse=True,
        )
        result_hits = []
        for i, h in enumerate(sorted_candidates[:top_k], start=1):
            result_hits.append(
                SearchHit(
                    doc_id=h.doc_id,
                    text=h.text,
                    score=scores.get(h.doc_id, 0.0),
                    rank=i,
                    source="rerank",
                    metadata=h.metadata,
                )
            )
        return result_hits


class CrossEncoderReranker:
    """本地 cross-encoder 精排（如 bge-reranker-v2-m3）。

    与 LLMReranker 的区别：
      - LLM rerank：把所有候选拼进一个 prompt，LLM 一次输出所有分数（贵、慢、灵活）。
      - cross-encoder：每个 (query, doc) 对单独送进小模型，输出一个相关度分数
        （便宜、快、确定性、但只能打分不能注入业务规则）。

    为什么用 cross-encoder 做对照？
      - 它是「双塔 vs 交叉编码」里交叉编码的标准实现，和你的双塔 embedder 形成完整对照。
      - bge-reranker-v2-m3 是 0.6B 多语言模型，和 Qwen3-Embedding-0.6B 同档，公平对比。
      - 本地推理，零 API 成本，适合做大量评测对比。
    """

    def __init__(
        self,
        model_id: str = "BAAI/bge-reranker-v2-m3",
        *,
        device: str | None = None,
        max_length: int = 512,
    ) -> None:
        self.model_id = model_id
        self.device = resolve_device(device)
        self.max_length = max_length
        self._model = None

    def _load(self) -> None:
        """懒加载 CrossEncoder 模型，只在第一次 rerank 时调用。

        为什么懒加载？
          - 和 embedder.py 一样：避免 import 时就触发 torch + 模型加载，
            让没装 torch 的环境也能 import 这个模块（比如跑 LLM rerank 的测试）。
        """
        if self._model is not None:
            return
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.model_id, device=self.device, max_length=self.max_length)

    def rerank(self, query: str, hits: list[SearchHit], *, top_k: int = 5) -> list[SearchHit]:
        """对 hits 按 query 相关度重排，返回 top_k。

        cross-encoder rerank 的核心步骤：
          1. 空输入兜底
          2. 去重（复用 _deduplicate_by_doc_id）
          3. 截断候选（和 LLM rerank 一样，避免推理过多）
          4. 构造 (query, doc) 对列表
          5. 调 model.predict(pairs) 拿到分数数组
          6. 按分数降序排序，构造新 SearchHit（source="cross_encoder"，rank 从 1 开始）

        和 LLM rerank 的关键差异：
          - 不需要降级逻辑：cross-encoder 是确定性模型，不会返回坏 JSON。
          - 不需要 _parse_scores：predict 直接返回 float 数组。
          - 分数是模型原始输出（可能是 logits，不一定是 0-1），不需要归一化也能排序。
        """
        if not hits:
            return []
        unique_hits = _deduplicate_by_doc_id(hits)
        candidates = unique_hits[: max(top_k * 2, 10)]
        self._load()
        pairs = [(query, h.text) for h in candidates]
        raw_scores = self._model.predict(pairs)
        scored = list(zip(candidates, raw_scores, strict=True))
        scored.sort(key=lambda x: x[1], reverse=True)
        result_hits = []
        for i, (h, score) in enumerate(scored[:top_k], start=1):
            result_hits.append(
                SearchHit(
                    doc_id=h.doc_id,
                    text=h.text,
                    score=float(score),
                    rank=i,
                    source="cross_encoder",
                    metadata=h.metadata,
                )
            )
        return result_hits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deduplicate_by_doc_id(hits: list[SearchHit]) -> list[SearchHit]:
    """按 doc_id 去重，保留第一次出现的顺序。"""
    seen: set[str] = set()
    result: list[SearchHit] = []
    for hit in hits:
        if hit.doc_id not in seen:
            seen.add(hit.doc_id)
            result.append(hit)
    return result


def _build_rerank_prompt(query: str, hits: list[SearchHit]) -> str:
    """构造给 LLM 的 rerank prompt。

    期望返回格式：只包含 JSON 对象，key 为 doc_id，value 为 0.0-1.0 的 float。

    为什么 prompt 要这样设计？
      - 一次性给所有候选，让 LLM 做相对比较，比逐对调用更省 token；
      - JSON 格式方便程序解析，避免自由文本里的不确定性。
    """
    lines: list[str] = [
        "You are a reranker. Score each candidate's relevance to the query.",
        f"Query: {query}",
        "Candidates:",
    ]
    for hit in hits:
        lines.append(f"- doc_id: {hit.doc_id}")
        lines.append(f"  text: {hit.text[:500]}")
    lines.append("Return ONLY a JSON object mapping doc_id to relevance score (0.0-1.0).")
    return "\n".join(lines)


def _parse_scores(content: str, valid_ids: list[str]) -> dict[str, float]:
    """解析 LLM 返回的 JSON 分数，过滤非法 doc_id。

    为什么过滤非法 doc_id？
      - LLM 可能幻觉出列表里没有的 doc_id，或拼写错误；
      - 程序只消费可信的 key，避免后续排序出错。
    """
    stripped = content.strip()
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    stripped = "\n".join(lines)
    try:
        raw = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {}
    valid_ids_set = set(valid_ids)
    result: dict[str, float] = {}
    for key, value in raw.items():
        if key in valid_ids_set:
            result[key] = float(value)
    return result
