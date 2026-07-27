"""检索评测指标：Recall@K 与 MRR。

【手写】填空题 — 把 pass 换成真实代码，不要让 AI 整块生成实现体。

================================================================================
背景：什么是 RAG？为什么这个文件跟 RAG 有关？
================================================================================

RAG = Retrieval-Augmented Generation（检索增强生成）。
它是当前 LLM 应用最主流的架构之一，核心思想是：

    用户问题
      -> 先去知识库里【检索】相关文档（retrieval）
      -> 把检索到的文档作为上下文【喂给 LLM】（augmentation）
      -> LLM 基于这些上下文【生成】答案（generation）

为什么需要 RAG？
  1. LLM 的知识是训练时固化的，无法访问你的私有数据（你的代码仓库、内部文档）。
  2. 直接把所有私有数据塞进 prompt 又会超上下文长度、成本爆炸。
  3. RAG 用"先检索再生成"的方式，让 LLM 只看相关的一小部分内容，既准又便宜。

典型 RAG 流程（也是本项目的流程）：
  ① 离线阶段：把项目素材切片 -> embedding -> 存入向量库 + 关键词索引。
  ② 在线阶段：用户问"哪些项目适合这份 JD"
     -> 把 JD 文本 embedding -> 在向量库 + BM25 索引里召回 top-K
     -> 用 RRF 融合两路结果（见 retrieval/hybrid.py）
     -> 用 LLM/cross-encoder rerank 精排（见 retrieval/rerank.py）
     -> 把 top-5 素材 + JD 一起喂给 LLM 写简历 bullet。

================================================================================
这个文件在 RAG 里扮演什么角色？
================================================================================

RAG 系统最容易出问题的环节就是【检索】：
  - 召回的相关文档太少 -> LLM 没素材，只能幻觉。
  - 召回的文档很多但相关项排得很后 -> LLM 看不到（上下文前面的内容权重更高）。

所以 RAG 系统必须配套【检索评测】，用数字回答：
  - "找得全不全" -> Recall@K
  - "排得准不准" -> MRR / NDCG

这个文件就是 RAG 评测层。没有它，调 RRF 参数、换 embedding 模型、加 rerank
都是凭感觉，无法量化"改了之后到底好了多少"。

================================================================================
为什么重要？
================================================================================

1. RAG 的生命线是检索质量。
   检索不行，生成再强也是垃圾进垃圾出（garbage in, garbage out）。
2. 评测是提示词/参数调优的前提。
   MVP_PLAN 反复强调："没有评测的调优是玄学"。
3. Recall@K / MRR 是信息检索（IR）面试高频考点，手写一遍才能真正理解。
4. Agent 工程的核心理念之一就是"指标驱动开发"：
   先用指标定位最弱环节（是召回不行还是排序不行），再针对性优化。

================================================================================
涉及哪些 Agent / RAG 开发知识点？
================================================================================

- RAG 评估方法论：
    * golden dataset：人工标注的 (query, expected_doc_ids) 对。
    * 离线指标：Recall@K、MRR、NDCG、Precision@K。
    * 在线指标：用户点击、停留时长、反馈（更真实但更贵）。
    * LLM-as-judge：用 LLM 评分生成质量（Phase 5 会用到）。
- 评测驱动开发流程：
    收集失败案例 -> 改提示词/参数 -> 跑回归评测集 -> 对比得分 -> 提交。
- 指标适用场景：
    * Recall@K：需要多篇素材的生成任务（写简历要 5 个项目）。
    * MRR：只取 Top-1 的场景（问答系统）。
    * NDCG：相关度有等级（高/中/低）时更精细。

================================================================================
面试可能会问什么？
================================================================================

Q1: 什么是 RAG？为什么不用 fine-tuning？
   A: RAG 是"检索 + 生成"，把外部知识动态注入 prompt；
      fine-tuning 是把知识固化进模型权重，并需要根据新知识重新训练模型。
      RAG 适合知识频繁更新、需要溯源、数据量大的场景；fine-tuning 适合改风格、改任务格式。

Q2: 为什么 RAG 系统要用 Recall@K 而不是 Accuracy？
   A: 检索任务通常返回一个列表，Accuracy 只看 Top-1 对不对；Recall@K 看前 K 个里
      有没有把相关文档都找出来，更符合 RAG 的实际使用场景（LLM 会读多条上下文）。

Q3: Recall@K 和 MRR 分别适合什么场景？
   A: Recall@K 衡量"找全"（用于需要多篇素材的生成任务，如本项目写简历）；
      MRR 衡量"第一个相关项排多靠前"（适合只取 Top-1 的问答场景）。

Q4: 如果 Recall@5 很低，应该怎么调？
   A: 先看是向量路还是 BM25 路漏了 -> 调 RRF 的 k -> 加 rerank ->
      换 embedding 模型 -> 检查切片粒度（chunk size）。

Q5: 你的 golden dataset 怎么来的？
   A: 用真实 JD + 项目素材，人工标注 expected_doc_ids；后续可扩展到 LLM-as-judge
      自动标注 + 人工抽查。

Q6: NDCG 和 MRR 有什么区别？
   A: MRR 只看第一个相关项的位置；NDCG 考虑所有相关项的位置和等级，更全面但更复杂。

================================================================================
第一步：实现下面的函数，然后跑 pytest tests/test_retrieval_metrics.py -q
================================================================================
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from repo2resume.storage.models import SearchHit


def compute_recall_at_k(
    hits: list[SearchHit],
    expected_doc_ids: set[str],
    *,
    k: int = 5,
) -> float:
    """Recall@K = 前 K 个结果中命中相关 doc 的比例。

    在 RAG 里的意义：
      如果 Recall@5 = 0.4，意味着 5 个真正相关的文档里，只有 2 个进了 top-5，
      剩下 3 个被排到 5 名之后，LLM 就看不到了，生成质量会受影响。

    公式：|{expected} ∩ top_k_doc_ids| / |{expected}|
    如果 expected 为空，返回 0.0 避免除零。

    示例：
        expected = {"A", "B"}
        hits = [A, C, D, E, B] (k=5)
        命中 2 个 -> recall = 2/2 = 1.0

    伪代码：
      if not expected_doc_ids: return 0.0
      top_k = {h.doc_id for h in hits[:k]}
      found = len(expected_doc_ids & top_k)
      return found / len(expected_doc_ids)
    """
    if not expected_doc_ids:
      return 0.0
    top_k = {h.doc_id for h in hits[:k]}
    found = len(expected_doc_ids & top_k)
    return found / len(expected_doc_ids)



def compute_mrr(
    hits: list[SearchHit],
    expected_doc_ids: set[str],
) -> float:
    """Mean Reciprocal Rank：第一个相关 doc 排名的倒数。

    在 RAG 里的意义：
      MRR = 1.0 表示第一个相关文档就排第 1，LLM 第一眼就能看到；
      MRR = 0.25 表示第一个相关文档排第 4，LLM 要看完 3 个噪声才看到有用信息。
      由于 LLM 对上下文前面内容更敏感（lost in the middle 现象），MRR 高的 RAG 质量更好。

    公式：1 / rank_of_first_relevant_doc
    如果全部都没相关，返回 0.0。

    示例：
        hits = [B, A, C], expected = {"A"}
        A 出现在第 2 位 -> mrr = 1/2 = 0.5

    伪代码：
      for rank, hit in enumerate(hits, start=1):
          if hit.doc_id in expected_doc_ids:
              return 1.0 / rank
      return 0.0
    """
    for rank, hit in enumerate(hits, start=1):
      if hit.doc_id in expected_doc_ids:
        return 1.0/rank
    return 0.0


def evaluate_search_results(
    queries: list[dict[str, Any]],
    search_fn: Any,
    *,
    k_values: Sequence[int] = (5,),
) -> dict[str, Any]:
    """批量跑评测：每个 query 调用 search_fn(query, top_k=max(k_values))，算 Recall@K / MRR。

    在 RAG 里的意义：
      这是"评测 runner"——一次跑完整个 golden dataset，输出平均指标。
      有了它才能做"改 RRF 参数 -> 跑评测 -> 对比 -> 提交"的迭代闭环。

    queries 格式（即 golden dataset）：
        [
            {"query": "Python backend", "expected_doc_ids": ["repo:foo:summary"]},
            ...
        ]

    为什么写一个批量函数？
      - 评测集通常有 10+ 条 query，手动一条条算太慢。
      - 批量函数可以输出"平均 Recall@5 / MRR"，方便对比不同策略。
      - 返回 dict 方便后续 CLI 渲染成对比表（Phase 5 的 evals run 命令）。

    伪代码：
      max_k = max(k_values)
      recalls = {k: [] for k in k_values}
      mrrs = []
      for q in queries:
          hits = search_fn(q["query"], top_k=max_k)
          expected = set(q["expected_doc_ids"])
          mrrs.append(compute_mrr(hits, expected))
          for k in k_values:
              recalls[k].append(compute_recall_at_k(hits, expected, k=k))
      return {
          "count": len(queries),
          "recall": {k: avg(recalls[k]) for k in k_values},   # 平均
          "recall_per_query": recalls,                          # 明细
          "mrr": avg(mrrs),                                     # 平均
          "mrr_per_query": mrrs,                                # 明细
      }
    """
    max_k = max(k_values)
    recalls = {}
    for k in k_values:
        recalls[k] = []
    mrrs = []
    for q in queries:
        hits = search_fn(q["query"], top_k=max_k)
        expected = set(q["expected_doc_ids"])
        mrrs.append(compute_mrr(hits, expected))
        for k in k_values:
            recalls[k].append(compute_recall_at_k(hits, expected, k=k))
    recall_avg = {}
    for k, v in recalls.items():
        recall_avg[k] = sum(v) / len(v) if v else 0.0
    mrr_avg = sum(mrrs) / len(mrrs) if mrrs else 0.0
    return {
        "count": len(queries),
        "recall": recall_avg,
        "recall_per_query": recalls,
        "mrr": mrr_avg,
        "mrr_per_query": mrrs,
    }