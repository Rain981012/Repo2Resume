# Phase B / Phase 3 实践复盘：检索层 + 职位匹配（面试复习稿）

> 项目：Repo2Resume  
> 阶段：Phase 3 — 检索层（RAG）+ 职位搜索 / 匹配  
> 对应：`MVP_PLAN.md` Phase 3；前置 `03_phase_2_5_lifecycle_hooks.md`  
> 验收日：2026-07-27（chat：分析 → 确认方向 → 多方向 mock 职位推荐）

## 1. 一句话定位

Phase 3 给 agent 装上「外部记忆」和「职位能力」：本地 embedding + ChromaDB/FTS5 混合检索 + RRF/rerank，再把职位 mock 源与画像打分接进 chat。面试可说：我手写了 hybrid retrieval 与评测指标，并搞清双塔 vs 交叉编码；职位侧先 mock 跑通匹配闭环，真联网搜岗留到 MVP 后。

## 1.5 Phase 3 的完整流程

Phase 3 有两条线：**构建顺序**（模块怎么叠起来）和 **运行时数据流**（用户点一下之后代码怎么走）。面试讲清这两条，比只背概念更有说服力。

### A. 构建顺序（为什么按这个顺序写）

```
① embedder.py + _device.py     —— 文本 → 向量（双塔）；device: cuda > mps > cpu
      │
② store.py                     —— ChromaDB（稠密）+ SQLite FTS5（稀疏）统一入库/检索
      │
③ hybrid.py 【手写】           —— 两路召回 → RRF 融合成一份 SearchHit 列表
      │
④ rerank.py 【手写】           —— 对 hybrid top 结果精排（LLM 或 Cross-Encoder）
      │
⑤ jobs/search.py               —— JobSource 协议：Mock 真跑 / Tavily stub
      │
⑥ jobs/matcher.py              —— 画像 ↔ 职位：向量分 + LLM 分 → MatchScore
      │
⑦ evals/retrieval_metrics.py 【手写】 —— Recall@K / MRR / evaluate_search_results
      │
⑧ builtins.py                  —— 包装成 search_jobs / find_project_materials 工具
      │
⑨ cli.py chat / jobs           —— 注册工具、系统提示、spinner/streaming
```

**依赖直觉：** 没有向量就没有 store；没有两路 hit 就没有 RRF；没有融合结果就没有 rerank；职位源与 matcher 相对独立，但都依赖画像（Phase 1）和 embedder。工具与 CLI 最后接，保证 harness（Phase 2）不用改 loop。

### B. 端到端用户流程（chat 主路径）

这是 Phase 3 验收时你实际跑通的路径：

```
用户：「开始，我是 Rain」
  │
  ├─ AgentLoop 调 analyze_repo（默认 stats_only=False）
  │     │
  │     ├─ mine 本地 ./local_repos/（作者过滤）
  │     ├─ tech_detector + fact_sheet
  │     ├─ Profiler → LLM 生成 SkillProfile
  │     │     primary_direction / secondary_directions / tech_stack / highlights…
  │     └─ 写入 SQLite skill_profiles
  │
  ├─ Agent 展示画像摘要，问：「要按这些方向搜职位吗？」
  │
用户：「好」
  │
  └─ AgentLoop 调 search_jobs（query 留空）
        │
        ├─ 读最新 SkillProfile
        ├─ queries = [primary] + secondary_directions   ← 多路
        ├─ 每路 MockJobSource.fetch（中英同义词扩展）
        ├─ 按 job.id 合并去重
        ├─ JobMatcher.match_all：
        │     encode(画像文本) ↔ encode(JD) → cosine
        │     + LLM 结构化 {score, reason}
        └─ 返回多条匹配职位 → Agent 用中文表格展示
```

可选下一步（同一会话）：

```
用户：「针对 Full Stack 找项目素材」
  │
  └─ find_project_materials(query=…)
        ├─ VectorStore.upsert_profile_materials(profile)  ← 画像亮点/one_liner 等入库
        ├─ hybrid_search(store, query)  ← 向量 + FTS5 → RRF
        ├─ LLMReranker.rerank(...)      ← 精排 top_k
        └─ 返回带 repo 标注的素材片段 → 供 Phase 4 写简历用
```

### C. RAG 检索流水线（找素材时内部发生了什么）

```
                    ┌─────────────────┐
  SkillProfile ──►  │ upsert_profile_ │  拆成 DocumentChunk
  (+ stats 可选)    │    materials    │  doc_id 例：
                    └────────┬────────┘    repo:socialdistribution:summary
                             │             profile:tech_stack
                             ▼
              ┌──────────────┴──────────────┐
              ▼                             ▼
       ChromaDB（稠密）              SQLite FTS5（稀疏）
       embedder.encode(text)         全文 MATCH
              │                             │
              └──────────┬──────────────────┘
                         ▼
              hybrid_search / RRF
              score = Σ 1/(k + rank)     k 常取 60
                         │
                         ▼
              rerank（LLM 或 Cross-Encoder）
                         │
                         ▼
              top_k SearchHit → 工具文本 / 评测 hits
```

**两阶段检索心智模型：**

| 阶段 | 做什么 | 本项目落点 |
| ---- | ------ | ---------- |
| 粗排 / 召回 | 从全库拉候选 | 双塔 embedding + FTS5，再 RRF |
| 精排 | 只对 top 候选重打分 | `LLMReranker` 或 `CrossEncoderReranker` |

### D. 职位匹配流水线（与 RAG 并行的另一条腿）

```
SkillProfile                    Job 列表（mock / 未来 tavily）
     │                                │
     ▼                                ▼
_build_profile_text            _build_job_text
     │                                │
     └──── embedder.encode 两端 ──────┘
                    │
                    ▼
            cosine → vector_score
                    │
                    ▼
            LLM JSON → llm_score + reason
                    │
                    ▼
         overall_score（加权/综合）排序 → Top-N 展示
```

注意：**职位匹配 ≠ 项目素材检索**。前者是「画像像不像这份 JD」；后者是「库里哪段经历能支撑写这条 bullet」。Phase 4 会把两者串起来：先选定职位，再 `find_project_materials`，再 Writer。

### E. CLI 对照（不经过 AgentLoop 时）

| 命令 | 等价流程 |
| ---- | -------- |
| `repo2resume analyze` | 挖仓 + 画像（同 analyze_repo，`stats_only=False`） |
| `repo2resume analyze --stats-only` | 只挖仓，不调 Profiler LLM（排障利器） |
| `repo2resume jobs` | 读最新画像 → primary+secondary 多路 mock 搜 → matcher → Rich 表 |
| `repo2resume chat` | 上表 B：自然语言驱动工具 |

### F. 数据落在哪

| 数据 | 位置 |
| ---- | ---- |
| SkillProfile | SQLite `skill_profiles` |
| 会话 / tool traces | SQLite sessions / tool_traces |
| 向量 | `~/.repo2resume/` 下 Chroma 持久化目录（由 store 管理） |
| 关键词索引 | SQLite `fts_documents` |
| 职位缓存 | SQLite `jobs.payload_json`（按 query+count 哈希） |
| 配置 | `~/.repo2resume/config.toml`（`embed_model` / `llm_model`） |

### G. 本阶段刻意没接进主流程的

- Tavily 真搜、职位 `url` 核实、Skill「技能 Top-N / 大厂待遇 Top-N」→ MVP 后  
- `repo2resume evals run` → Phase 5；现在用指标函数 + 自改 golden 脚本跑  

## 2. 本阶段完成了什么（对照计划）

| 计划项 | 交付物 | 状态 |
| ------ | ------ | ---- |
| Embedder | `retrieval/embedder.py`（Qwen3-Embedding-0.6B 本地）+ `_device.py`（cuda>mps>cpu） | ✅ |
| Store | `retrieval/store.py`（ChromaDB + SQLite FTS5）+ DB migration v3/v4 | ✅ |
| Hybrid / RRF | `retrieval/hybrid.py`（【手写】`reciprocal_rank_fusion`） | ✅ |
| Rerank | `retrieval/rerank.py`：`LLMReranker` + `CrossEncoderReranker`（【手写】） | ✅ |
| 职位源 | `jobs/search.py`：mock 源可用；Tavily stub（未真接） | ✅ mock / ⏳ 联网 |
| Matcher | `jobs/matcher.py`：向量相似度 + LLM 结构化打分 | ✅ |
| Agent 工具 | `builtins`：`search_jobs` / `find_project_materials` | ✅ |
| CLI | `repo2resume jobs`；chat 接线 + streaming/spinner | ✅ |
| 评测指标 | `evals/retrieval_metrics.py`（【手写】Recall@K / MRR）+ golden 占位 | ✅ 函数 / ⏳ 真跑 |
| `evals run` CLI | Phase 5 stub | ⏳ |

交付物路径：

- `src/repo2resume/retrieval/` — embedder / store / hybrid / rerank / `_device`
- `src/repo2resume/jobs/` — search / matcher
- `src/repo2resume/evals/retrieval_metrics.py`
- `evals/datasets/retrieval_golden.json`（占位，需改成真实 repo doc_id）
- `src/repo2resume/agent/builtins.py` — Phase 3 工具；空 query 时 primary+secondary 多路搜
- `src/repo2resume/cli.py` — `jobs` / chat 系统提示（分析后确认再搜）
- `tests/test_retrieval_*.py` / `tests/test_jobs_*.py` / `tests/test_retrieval_metrics.py`

**搭建完成 vs 验收：**

- ✅ chat 真跑：analyze → 问确认 → 返回 5 条跨方向 mock 职位并打分  
- ✅ `analyze` 生成完整 SkillProfile（含 primary / secondary）  
- ⏳ Tavily / 真联网岗、职位 URL、Skill 两套 Top-N（技能 / 大厂待遇）— MVP 后  
- ⏳ golden 换成自己的 doc_id 后跑 Recall@5 ≥ 0.8；`repo2resume evals run` 属 Phase 5  

## 3. 做法 · 影响 · 亮点 · 踩坑

### 3.1 做了什么

- 本地加载 Qwen3-Embedding-0.6B，向量入库 ChromaDB，文本进 FTS5；`hybrid_search` 双路召回后 RRF 融合。
- 手写 `LLMReranker`（零样本 JSON 打分）与 `CrossEncoderReranker`（bge-reranker-v2-m3）对照。
- 手写 `compute_recall_at_k` / `compute_mrr` / `evaluate_search_results`。
- mock 职位源 + `JobMatcher`（cosine + LLM score）；工具注册进 chat。
- 默认 embedding：`local:Qwen/Qwen3-Embedding-0.6B`（放弃偏英文小模型）；device 支持 MPS（Apple Silicon）。
- chat：`analyze_repo` 默认 `stats_only=False`；`search_jobs` 空 query 覆盖 primary+secondary；mock 中英同义词扩展。
- UX：chat spinner + LLM streaming；卡死检测 `max_repeated_tool`。

### 3.2 对项目的影响

- Agent 不再只靠上下文里那点统计：写简历前可按 JD **检索**项目素材（`find_project_materials`）。
- 职位推荐闭环可演示：画像 → 匹配分 → 理由；不依赖外网即可验收 harness + RAG 接线。
- 为 Phase 4 Writer 准备「可溯源素材」入口；为 Phase 5 评测准备指标函数与 golden 骨架。

### 3.3 亮点（可上面试）

1. **Hybrid + RRF**：稀疏（关键词）补专有名词，稠密（向量）补语义；RRF 只看名次不看不可比的原始分，实现极简。
2. **双塔 vs 交叉编码对照**：embedder 双塔粗排、cross-encoder 精排；LLM rerank 灵活但贵且非确定性（`temperature=0`）。
3. **懒加载重依赖**：`torch` / `transformers` / CrossEncoder 延迟 import，LLM rerank 测试可不装 GPU 栈。
4. **排障分层**：chat「卡死」表面是提示词/卡死检测，根因是 `stats_only=False` 路径上 Profiler 调 LLM 挂起；`--stats-only` 秒出对照证明挖仓正常。

### 3.4 踩坑

**坑 1：把「重复调工具」误判成模型弱 / 提示词太长**

- 现象：复杂提示词要求 `stats_only=False` 后，连续 5 次 `analyze_repo` 触发卡死检测。
- 根因：`Profiler.build_profile` 再调 LLM 时 API 挂起/失败 → `ErrorRecoveryHook` 回传「请重试」→ 强模型**正确重试**。
- 教训：卡死检测只看工具名，不区分「成功后重复」和「失败后重试」；先对照 `--stats-only` / 直跑 `analyze` 暴露真异常。
- 面试升华：agent 重试是合理行为；要区分瞬时故障与死循环，应用错误标记或结果签名，不能只看 name。

**坑 2：配置里的 embed 模型与文档默认不一致**

- 现象：`config.toml` 仍是 `gemini/text-embedding-004`，本地 Qwen 路径失败，`search_jobs` 连环失败。
- 教训：改 `DEFAULT_EMBED_MODEL` 不会自动改用户已有 toml；冷启动要核对 `~/.repo2resume/config.toml`。

**坑 3：空 query 只搜 primary → 只像「一个方向一条岗」**

- 现象：画像有 secondary，但推荐覆盖窄；中文「后端」几乎只靠 token `python` 命中英文 JD。
- 修复：primary+secondary 多路搜合并；mock 加中英同义词（后端→backend/django…）。

**坑 4：eager import torch 拖垮无关测试**

- 现象：`import retrieval` 或测 hybrid 时 `ModuleNotFoundError: torch`。
- 修复：`retrieval/__init__.py` 不重导出；embedder / CrossEncoder / `resolve_device` 懒导入。

**坑 5：analyze / chat 长时间无进度像「卡死」**

- 现象：选完作者后只打一行 Redis log，其实在等 LLM。
- 教训：长路径必须有阶段提示（mining / calling LLM）；streaming/spinner 解决的是可感知性，不是加速。

**坑 6：RRF** `scores[doc_id] +=` **未初始化 →** `KeyError`

- 教训：累计分用 `defaultdict(float)` 或 `setdefault`。

## 4. 本阶段知识点（面试向）

每个技术点按**同一套五问**答（临场可直接套）：

1. **X 是什么？**
2. **为什么要在 Repo2Resume（这个项目）里用它？**
3. **为什么不用……（更偷懒 / 更常见的替代做法）？**
4. **有什么优缺点？**
5. **为什么不用其他同类技术 / 竞品？**

| 技术点 | 本项目落点 |
| ------ | ---------- |
| RAG | 画像素材入库 → 按 JD 检索 →（Phase 4）再生成 |
| 本地 Embedding（双塔） | Qwen3-0.6B + `Embedder.encode` |
| ChromaDB | 稠密向量存储 |
| SQLite FTS5 | 稀疏关键词检索 |
| Hybrid + RRF | 两路召回再按名次融合 |
| LLM / Cross-Encoder 精排 | `LLMReranker` 主路径；CE 对照 |
| Recall@K / MRR + Golden | 离线评测检索 |
| Mock JobSource + Matcher | 职位匹配闭环 |
| Tool 接入 AgentLoop | `search_jobs` / `find_project_materials` |

---

### ① RAG（Retrieval-Augmented Generation）

**1. RAG 是什么？**  
检索增强生成：问题（或 JD）来了之后，先去**外部知识库**查出相关文档片段，再把片段当上下文交给 LLM 生成。知识可更新、可替换，不全靠模型参数里的「死记」，用来降低细节幻觉。

**2. 为什么要在这个项目用它？**  
写简历要「按目标 JD 挑项目经历」。经历散落在多仓的统计、README、亮点池里。RAG 把「找哪几段证据」做成可检索、可评测的系统能力。  
别人 fork 后分析的是**他们自己的仓**：每人本地建库——解决的是「私有贡献太多、要按查询切片」，不是共享公网语料。所以即使用户场景是本地 CLI，仍然需要 RAG。

**3. 为什么不用「把全部 git 统计 / README 塞进超长 context」？**  
多仓一多就爆窗或极贵；无法按不同 JD 动态取舍；没法做 Recall/MRR 离线回归。长 context 适合「单仓、一次对话」；本项目是「多仓素材池 × 多 JD」。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| token 可控；知识可更新（重 analyze + upsert） | 多一套索引与冷启动（下模型、建库） |
| 可评测、可溯源（bullet ↔ SearchHit） | 检索错了，生成也会错 |
| 同一画像可服务多个 JD | 工程比「纯 LLM 问答」重 |

**5. 为什么不用其他替代技术？**  
- **Fine-tune 小模型记住用户经历：** 每用户一份模型不现实，仓一变就要重训。  
- **只靠 Chat session 记忆：** 关进程就没了，也装不下全量证据。  
- **全文规则关键词匹配：** 无语义，同义 JD 打不开。  
RAG = 检索系统 + 生成，比单一手段完整。

---

### ② 双塔 Embedding / 本地 Qwen3-Embedding-0.6B

**1. 是什么？**  
双塔（Bi-encoder）：编码器**分别**把 query、document 编成固定维向量（本项目 1024 维），用余弦相似度比远近。doc 向量不依赖 query，可离线预计算进向量库。

**2. 为什么要在这个项目用它？**  
稠密检索和职位语义匹配都需要 embedding。默认本地 0.6B：中英+代码够用，符合「本地 CLI、fork 可跑」；Apple Silicon 走 MPS。

**3. 为什么不用「每次让 LLM 读完全库再挑相关段落」？**  
太贵太慢，无法预计算，也难离线评测。双塔：一次 encode query + 近邻搜索即可粗排。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 可预计算、延迟低、可规模化 | query-doc 交互弱 |
| 本地：零按次费、数据不出机 | 冷启动要下权重；精度常不如更大模型/强 API |
| 与职位匹配共用同一向量思想 | 短专有名词有时不如关键词准 |

**5. 为什么不用其他同类技术？**  
- **默认 OpenAI text-embedding-3-small：** 要 key、费用、隐私门槛（可配置切换，不作默认）。  
- **默认 Qwen3-4B：** 更准但体积/RAM 重，伤冷启动。  
- **词袋 / 古典 TF-IDF「向量」：** 无深层语义，跨语言差。  
- **偏英文的超小 embedding 当默认：** 中文+代码简历场景弱（所以放弃 bge-small-en 路线）。

---

### ③ ChromaDB（向量库）

**1. 是什么？**  
嵌入式向量数据库：存 embedding + 原文/metadata，支持相似度查询与本地持久化。

**2. 为什么要在这个项目用它？**  
双塔粗排需要「按向量查近邻」。Chroma 与 Python/单机 CLI 同档，id、文本、metadata 一起管，胶水少。

**3. 为什么不用「把 numpy 向量 pickle 进文件自己 brute-force」？**  
素材变多要自写索引、过滤、持久化，易脏。向量库把「存 + 查」标准化。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 少运维、接入快 | 不是海量分布式引擎 |
| 元数据与文本同管 | 高级性能调优不如专用系统 |

**5. 为什么不用其他同类技术？**  
- **Milvus / Qdrant 集群：** 额外服务，MVP ROI 低。  
- **纯 FAISS：** 更偏库，id↔文本要自管。  
- **pgvector：** 主存储已是 SQLite，再拉 Postgres 过重。

---

### ④ SQLite FTS5（稀疏 / 关键词检索）

**1. 是什么？**  
SQLite 内置全文检索（BM25 族）：按词是否出现、词频、稀有度打分，偏「字面命中」。

**2. 为什么要在这个项目用它？**  
JD/技术栈里大量专有名词（FastAPI、仓库名）。稀疏一路补双塔在「必须字面命中」上的短板；项目已有 SQLite，零新进程。

**3. 为什么不用「Python 里 `if keyword in text` 扫全表」？**  
无排名、无 IDF、数据一大就慢；FTS 是正经倒排索引。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 部署极简、和业务库同备份 | 中文分词/调参不如 ES 生态 |
| 专有名词召回强 | 同义改写弱（所以要 hybrid） |

**5. 为什么不用其他同类技术？**  
- **Elasticsearch / OpenSearch：** 又一个服务，违背 `pip install` 本地跑。生产 QPS 上来可换，接口仍是「返回 ranked hits」。  
- **独立 Lucene / BM25 服务：** 同理过重。

---

### ⑤ Hybrid 检索 + RRF 融合

**1. 是什么？**  
- **Hybrid：** 同时跑稠密（向量）和稀疏（FTS）两路召回。  
- **RRF：** 对每个 doc 按各路名次累加 `1/(k+rank)`（k 常取 60），再排序——**只看名次，不看原始分**。

**2. 为什么要在这个项目用它？**  
简历素材既有「必须命中的技术词」，又有「说法不同、意思相近」的描述。单路有盲区；RRF 避开 BM25 分与余弦分不可比的问题。

**3. 为什么不用「只用向量」或「只用 BM25」？**  
- 只用向量：Kubernetes 等词可能被语义近邻挤掉。  
- 只用 BM25：高并发≈QPS、中英混写易 miss。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 互补，召回更稳 | 实现与评测多一条路径 |
| RRF 实现极简、少超参 | RRF 丢掉「分差大小」信息 |
| 面试高频、好讲 | 仍需精排降噪声 |

**5. 为什么不用其他融合技术？**  
- **归一化后加权求和：** 要归一化 + 调 α，小 golden 易过拟合。  
- **Cascade（先 BM25 再向量）：** 对「BM25 全 miss、向量能救」不友好；RRF 两路更对称。  
- **Learning-to-Rank：** 要大量标注与训练，超 MVP。

---

### ⑥ 精排：LLM Rerank 与 Cross-Encoder

**1. 是什么？**  
对粗排 top-k 再打相关度：  
- **LLM rerank：** 候选进 prompt，输出 JSON 分数。  
- **Cross-encoder：** `(query, doc)` 联合编码，输出相关度分数。

**2. 为什么要在这个项目用它？**  
粗排仍有噪声；写错经历成本高。主路径用 **LLMReranker**（与 agent 同 LiteLLM 栈）；**CrossEncoderReranker** 作对照，吃透双塔 vs 交叉编码。

**3. 为什么不用「粗排 top-5 直接当最终结果」？**  
简历错检贵；工业上也是「召回宽、精排窄」。

**4. 有什么优缺点？**  

| | LLM rerank | Cross-encoder |
| -- | -- | -- |
| 优点 | 灵活、可加业务规则、无额外小模型 | 稳、本地相对便宜、适合离线大批对比 |
| 缺点 | 贵、慢、坏 JSON、确定性一般 | 难注入规则；要再下一套权重 |

**5. 为什么不用其他精排技术？**  
- **全程 Cross-encoder 扫全库：** 算不动，必须先粗排。  
- **ColBERT 等晚交互：** 更强但实现更重，超当前学习范围。  
- **只实现一种：** 少对照；两种都写是为了面试讲清取舍，不是两套同时打满生产。

---

### ⑦ Recall@K、MRR 与 Golden Dataset

**1. 是什么？**  
- **Golden：** 人工标的 `(query → expected_doc_ids)`。  
- **Recall@K：** 相关文档里有多大比例进了前 K。  
- **MRR：** 第一条相关文档排名 r 的 `1/r` 再平均。

**2. 为什么要在这个项目用它？**  
改 embedding / RRF k / 是否 rerank，不能靠「感觉」。Phase 3 验收之一是 Recall@5 ≥ 0.8。简历要多段素材 → 优先盯 Recall@K。

**3. 为什么不用「只人工抽几条感觉」或「只报 Accuracy@1」？**  
感觉不可回归；Accuracy@1 对多相关文档过苛，不反映「找全」。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 可对比、可回归 | golden 要人工标；占位数据无意义 |
| 指标语义清晰 | Recall 高 ≠ 生成一定好 |

**5. 为什么不用其他指标当唯一标准？**  
- **NDCG：** 更细，但要 graded 标注，成本高；可后续加。  
- **只 MRR：** 忽略还需要第 2～5 条素材。本项目两者都算，主叙事用 Recall@5。

---

### ⑧ Mock 职位源 + 向量/LLM 匹配

**1. 是什么？**  
- **MockJobSource：** 内存固定若干 JD，实现 `JobSource.fetch`。  
- **JobMatcher：** 画像↔JD 双塔相似度 + LLM 结构化打分与中文理由。  
匹配回答「适不适合投」；`find_project_materials` 回答「用哪段经历写」——两条腿，Phase 4 才串起来。

**2. 为什么要在这个项目用它？**  
先验收「画像 → 打分 → 多方向展示」闭环，不绑外网；`JobSource` 协议保证以后换真源不改 matcher。

**3. 为什么不用「一上来爬 Boss / 接 Tavily」？**  
反爬、过期岗、列表页 vs 详情、合规会淹没 harness/RAG 学习目标。Skill 大厂两套 Top-N 是产品完整度，刻意后置。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 单测稳、可演示 | 不能当真求职 |
| 接口清晰可替换 | 无真实 url/时效 |

**5. 为什么不用其他匹配技术？**  
- **纯技能集合交并比：** 可解释但同义词/职级脆弱。  
- **纯 LLM 打分：** 贵、难回归。  
- **纯向量：** 缺「缺什么技能」的叙述。  
→ **向量稳信号 + LLM 理由**。

---

### ⑨ 把能力做成 Agent Tool（接入手写 Loop）

**1. 是什么？**  
`search_jobs` / `find_project_materials` 注册进 `ToolRegistry`，由 `AgentLoop` 决定何时调用。

**2. 为什么要在这个项目用它？**  
与 Phase 2 harness 一致：扩能力 = register，不改 loop；用户可自然语言分支（先看某仓 / 先搜岗）。

**3. 为什么不用 chat 里写死 `if 用户说搜岗: ...`？**  
那是脚本不是 agent；难测、难扩展；面试讲不清 harness。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 可组合、可单测工具 | 多一轮 LLM；可能乱调/重复调 |
| 与 Phase 4 子 agent 同构 | 需要提示词 + 卡死检测兜底 |

**5. 为什么不用 LangChain AgentExecutor 等框架直接接？**  
本项目目标是吃透手写 loop；框架内核黑盒。工具抽象可与框架同构，但控制流自己握着。

---

### ⑩ 补充：工程取舍也用同一套问法

**卡死检测（按工具名）**  
1. **是什么：** 连续同名工具调用 ≥ N 次则停。  
2. **为什么用：** 几行挡住明显死循环。  
3. **为什么不用更复杂签名：** MVP 先简单；但我们知道会误杀「失败重试」。  
4. **优缺点：** 简单好测 / 不区分成功重复与失败重试。  
5. **更好替代：** 签名含 arguments，或「连续失败且错误文本相似」才算卡死。

**懒加载 torch / 模型**  
1. **是什么：** 真正 `_load`/`encode`/`rerank` 才 import 重依赖。  
2. **为什么用：** 测 RRF/指标不必装 GPU 栈。  
3. **为什么不用启动即 load：** 冷启动与 CI 都痛。  
4. **优缺点：** import 轻 / 首次推理更慢一点。  
5. **替代：** 独立 worker 进程常驻模型——产品化可做，MVP 过重。

---

### 速记对照表

| 我用了 | 为什么不用常见替代 | 一句话 |
| ------ | ------------------ | ------ |
| RAG | 超长 context / 微调记经历 | 多仓×多 JD，可检索可评测 |
| 本地 0.6B | OpenAI embed / 4B | 冷启动+隐私；精度可升级 |
| Chroma | Milvus / 纯 FAISS | 单机够用、元数据省心 |
| FTS5 | Elasticsearch | 零新服务 |
| Hybrid+RRF | 单路 / 加权分融合 | 互补 + 避开量纲 |
| LLM 精排+CE 对照 | 不精排 / 全库 CE | 降噪声；对照学完差异 |
| Mock+Matcher | 先爬真岗 / 纯规则 | 先闭环；接口可替换 |
| Agent Tool | 写死 if-else / 黑盒框架 | harness 一致、可讲清 |

## 5. 下一步

1. （可选）把 `retrieval_golden.json` 改成自己的 `repo:*` doc_id，脚本跑 `evaluate_search_results`，盯 Recall@5。  
2. （可选）commit Phase 3 复盘与代码。  
3. **进入 Phase 4**：Writer / Critic、溯源 bullet、`resume`/`export`、子 agent。  
4. 真联网搜岗 + 超链接 + 大厂两套 Top-N → MVP 后增强（接 Tavily 或同等源）。

## 6. 30 秒速记卡

- **做了什么：** 本地 embedding + hybrid/RRF/rerank + mock 职位匹配接进 chat；手写检索指标。  
- **Gate：** chat 能分析并推荐多方向职位；单元测试绿；联网岗与 evals CLI 未完成。  
- **一句话：** RAG = 检索再生成；粗排双塔、精排交叉/LLM；mock 验收 harness，真搜留给产品化。  
- **面试答法：** 见 §4 — 每个技术都按「是什么 → 为什么用 → 为什么不用… → 优缺点 → 为什么不用竞品」。  
- **流程：** 见 §1.5。  
- **踩坑：** 卡死其实是 LLM 失败重试；embed 配置漂移；只搜 primary；eager torch；无进度假死。  
- **下一站：** Phase 4 简历生成；golden 真跑评测可选。
