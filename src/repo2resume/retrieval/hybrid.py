"""混合检索：BM25 关键词 + 向量语义，RRF 融合。

【手写】填空题 — 把 pass 换成真实代码，不要让 AI 整块生成实现体。

================================================================================
背景：什么是 RAG？这个文件在 RAG 里扮演什么角色？
================================================================================

RAG = Retrieval-Augmented Generation（检索增强生成）。
核心思想：用户问题 -> 先去知识库检索相关文档 -> 把文档作为上下文喂给 LLM 生成答案。

RAG 的"检索"环节有两种主流技术路线：

  1. 稀疏检索（Sparse Retrieval）—— BM25 / TF-IDF
     - 原理：基于词频（TF）和逆文档频率（IDF），给每个词算权重。
     - 优点：精确匹配关键词，对专业术语（Kubernetes、FastAPI）命中率高，无需训练。
     - 缺点：不理解语义，"高并发"匹配不到"QPS/压测"。
     - 本项目用 SQLite FTS5 实现（零依赖，SQLite 内置）。

  2. 稠密检索（Dense Retrieval）—— Embedding + 向量数据库
     - 原理：用模型把文本编码成稠密向量，余弦相似度衡量语义距离。
     - 优点：理解语义，"分布式系统经验"能匹配到"高并发消息队列"。
     - 缺点：对低频专业名词不敏感，需要预训练模型 + 向量库。
     - 本项目默认用 Qwen3-Embedding-0.6B + ChromaDB（可切 Qwen3-Embedding-4B）。

hybrid.py 是 RAG 检索环节的【融合层】：把两路结果合并成一份排序列表。
为什么需要融合？因为两路各有短板：
  - 只用向量：JD 要 "Kubernetes"，向量可能召回"容器化经验"但排不到前面。
  - 只用 BM25：用户写"高并发"，关键词 miss "QPS/TPS/压测"等同义表达。
融合后能同时享受"精确匹配 + 语义理解"，是生产 RAG 的标配。

================================================================================
这部分是做什么用的？
================================================================================

Agent 要回答"我的哪些项目经历适合这份 JD"，第一步是**从项目素材池里召回相关文档**。
本项目同时维护两条检索通道：
  - 向量检索（ChromaDB）：靠 embedding 语义相似度。
  - 关键词检索（SQLite FTS5）：靠 BM25 词频-逆文档频率。

hybrid.py 负责把两路结果融合成一份排序列表，交给下一阶段的 rerank / matcher。

================================================================================
为什么重要？
================================================================================

1. RAG 的检索质量直接决定生成质量。
   召回不全 -> LLM 没素材 -> 幻觉；召回不准 -> LLM 看错素材 -> 跑题。
2. 混合检索是工业界 RAG 的最佳实践。
   单一检索方式都有盲区，融合后召回率和精确率都更高。
3. RRF 融合实现只有几行，却是 IR 经典方法，面试高频考点。
4. 这是理解"稀疏 vs 稠密检索互补性"最好的实践落点。

================================================================================
涉及哪些 Agent / RAG 开发知识点？
================================================================================

- RAG 检索架构：
    * 稀疏检索（BM25/TF-IDF）：关键词匹配，可解释，无需训练。
    * 稠密检索（embedding）：语义相似，需要模型 + 向量库。
    * 混合检索：两路召回 + 融合，取长补短。
- 多路召回融合策略：
    * RRF（Reciprocal Rank Fusion）：只看排名，无量纲，实现简单。
    * 加权融合：需要归一化和调权重，复杂但可定制。
    * 级联融合：一路的 top 结果优先，另一路补漏。
- Agent 工具设计：
    * 把检索封装成工具，让 LLM 决定查什么、查多少。
    * 工具返回结构化数据（SearchHit），方便 LLM 理解和后续 rerank。
- 容错设计：
    * 检索失败时不应让 agent 崩溃，返回空列表让上层降级处理。
    * 体现"确定性代码兜住概率性模型"的 harness 思想。

================================================================================
面试可能会问什么？
================================================================================

Q1: 什么是 RAG？它的检索环节有哪几种技术路线？
   A: RAG = 检索增强生成。检索环节有稀疏检索（BM25，关键词匹配）和稠密检索
      （embedding，语义相似）两种；生产系统常做混合检索 + 融合。

Q2: 为什么需要混合检索，而不是只用向量检索？
   A: 向量擅长语义相似，但容易 miss 精确术语和低频专业名词；BM25 擅长精确匹配。
      两者互补，融合后召回率和精确率都更高。

Q3: RRF 是怎么工作的？为什么用 RRF 而不是把向量分数和 BM25 分数直接相加？
   A: RRF 只看排名，按 1/(k+rank) 累加；不同检索通道的原始分数量纲不同，直接相加需要
      调权重和归一化，RRF 天然无量纲、对参数不敏感、实现简单。

Q4: 参数 k=60 是什么意思？调大或调小会怎样？
   A: k 是 RRF 的平滑参数；k 越大，排名差距对分数影响越小，结果越"平均"；
      k 越小，top 排名的优势越明显，但可能放大噪声。原论文用 60，是经验最优。

Q5: 如果模型反复调用同一个检索工具，你怎么防卡死？
   A: 在 agent loop 里做 max_rounds 和重复工具调用检测；检索工具本身只做只读查询，
      不修改状态，所以重复调用无害，但要控制 token 和成本。

Q6: 检索失败时应该抛异常还是返回空列表？
   A: 在 agent 内部通常返回空列表 + 记录日志，让 LLM 决定是重试、换 query 还是直接回答。
      这就是"工具异常回传给 LLM 重试"的 error recovery 模式。

Q7: 怎么评估混合检索比单路好？
   A: 用 golden dataset 算 Recall@5 / MRR，对比"只用向量"、"只用 BM25"、"混合"三种策略。
      本项目 Phase 3 验收标准就是 Recall@5 ≥ 0.8。

================================================================================
第一步：实现下面的三个函数，每填完一个跑 pytest tests/test_retrieval_hybrid.py -q
================================================================================
"""

from __future__ import annotations

from repo2resume.retrieval.store import VectorStore
from repo2resume.storage.models import SearchHit


def reciprocal_rank_fusion(
    vector_hits: list[SearchHit],
    keyword_hits: list[SearchHit],
    *,
    top_k: int = 10,
    k: int = 60,
) -> list[SearchHit]:
    """融合向量与关键词两路结果，返回 RRF 排序后的 top_k。

    在 RAG 里的位置：
      这是混合检索的【融合函数】。两路检索各自返回 top-N（带 rank），
      RRF 按 1/(k+rank) 给每路打分，同一文档在两路都出现就累加分数。
      最终按总分降序，取 top_k 交给 rerank。

    输入示例：
        vector_hits = [
            SearchHit(doc_id="A", text="...", score=0.9, rank=1, source="vector"),
            SearchHit(doc_id="B", text="...", score=0.8, rank=2, source="vector"),
        ]
        keyword_hits = [
            SearchHit(doc_id="B", text="...", score=2.1, rank=1, source="keyword"),
            SearchHit(doc_id="C", text="...", score=1.5, rank=2, source="keyword"),
        ]

    期望输出（k=60, top_k=10）：
        [
            SearchHit(doc_id="B", ..., score=1/61 + 1/62, rank=1, source="hybrid"),
            SearchHit(doc_id="A", ..., score=1/61,        rank=2, source="hybrid"),
            SearchHit(doc_id="C", ..., score=1/62,        rank=3, source="hybrid"),
        ]
    （B 在两路都出现，分数最高，排第 1。）

    注意：
      - 用 hits 里的 rank 字段（从 1 开始），不是 score 字段。
        原因：不同检索通道的 score 量纲不同（向量是 0-1 相似度，BM25 是 TF-IDF 分数），
        RRF 只看排名就是为了避开量纲问题。
      - 输出 SearchHit 的 source 统一为 "hybrid"，rank 按 RRF 新排序从 1 开始。
      - text 取任意一路的 text（两路相同 doc_id 的 text 应该一样）。
      - 用 dict 聚合分数，然后排序，最后取 top_k。

    伪代码：
      scores = {}  # doc_id -> 累计 RRF 分数
      text_map = {}  # doc_id -> text
      for hit in vector_hits:
          scores[hit.doc_id] += 1.0 / (k + hit.rank)
          text_map.setdefault(hit.doc_id, hit.text)
      for hit in keyword_hits:  # 同上
      sorted_ids = sorted(scores, key=scores.get, reverse=True)
      return [SearchHit(...) for i, doc_id in enumerate(sorted_ids[:top_k], start=1)]
    """
    from collections import defaultdict

    scores = defaultdict(float)  # 访问不存在的 key 自动返回 0.0
    text_map = {}
    meta_map = {}
    for hit in vector_hits:
        scores[hit.doc_id] += 1.0 / (k + hit.rank)
        text_map.setdefault(hit.doc_id, hit.text)
        if hit.metadata and not meta_map.get(hit.doc_id):
            meta_map[hit.doc_id] = hit.metadata
    for hit in keyword_hits:
        scores[hit.doc_id] += 1.0 / (k + hit.rank)
        text_map.setdefault(hit.doc_id, hit.text)
        if hit.metadata and not meta_map.get(hit.doc_id):
            meta_map[hit.doc_id] = hit.metadata
    sorted_ids = sorted(scores, key=scores.get, reverse=True)
    result = []
    for i, doc_id in enumerate(sorted_ids[:top_k], start=1):
        result.append(
            SearchHit(
                doc_id=doc_id,
                text=text_map[doc_id],
                score=scores[doc_id],
                metadata=meta_map.get(doc_id, {}),
                rank=i,
                source="hybrid",
            )
        )
    return result


def hybrid_search(
    store: VectorStore,
    query: str,
    *,
    top_k: int = 10,
    vector_top: int = 20,
    keyword_top: int = 20,
    k: int = 60,
) -> list[SearchHit]:
    """一站式混合检索：调用 store 的向量/关键词检索，再 RRF 融合。

    在 RAG 里的位置：
      这是 agent 工具 find_project_materials 内部调用的【检索入口】。
      给定 query（通常是 JD 的关键职责），返回 top-K 项目素材给 LLM 写简历。

    参数设计：
      - top_k：最终返回多少条（给 rerank / LLM 用）。
      - vector_top / keyword_top：每路先召回多少条再融合。
        通常设为 top_k 的 2-4 倍，保证融合后有足够候选。
      - k：RRF 平滑参数，默认 60（原论文经验值）。

    空查询或异常时返回 []，不要抛错给上层。

    为什么这里要 try-except？
      - 检索依赖外部存储（ChromaDB/SQLite），可能抛错；
      - agent 调用工具时，工具异常通常由 ErrorRecoveryHook 兜底，但这里直接返回空
        列表更简洁，避免让 LLM 看到一堆 traceback。
      - 体现"工具内部降级，不让 agent 卡住"的设计原则。

    伪代码：
      if not query.strip(): return []
      vector_hits = store.vector_search(query, top_k=vector_top)
      keyword_hits = store.keyword_search(query, top_k=keyword_top)
      return reciprocal_rank_fusion(vector_hits, keyword_hits, top_k=top_k, k=k)
    """
    if not query.strip():
        return []
    vector_hits = store.vector_search(query, top_k=vector_top)
    keyword_hits = store.keyword_search(query, top_k=keyword_top)
    return reciprocal_rank_fusion(vector_hits, keyword_hits, top_k=top_k, k=k)
