# Repo2Resume MVP 计划

> 依据 `DESIGN.md` 制定。目标：交付一个完整闭环的 CLI agent——分析本地仓库 → 技能画像 → 职位推荐 → 定制简历 → 对话修改 → 导出 Markdown。
>
> 总预估：**18-27 个工作日**（阶段 A 约 2 天，阶段 B 约 16-25 天）。每个 Phase 结束都有可运行、可演示的产物。

---

## 0. 编码方式图例（学习优先）

每个任务标注一种编码方式，取舍标准是「这段代码能否教会我 AI Agent 的核心知识」：

- **【手写】**：项目的技术核心与学习点。**不是裸写**——默认走「脚手架教练」：AI 拆步 + 给签名/伪代码/失败测试，你填实现；卡住再看参考并重写。禁止 AI 直接整块生成实现体。
- **【AI 辅助】**：你未必知道该设计什么——先一起做设计陪练（职责说明 + 接口/schema 草案 → 你确认），再由 AI 填实现；你要逐行看懂并能改。
- **【AI 生成】**：纯样板，手写学不到 Agent 知识。让 AI 一把生成，你只需会看会改。

> 一句话原则：`agent/` 目录整个【手写】（教练陪练）；`retrieval/` 与多智能体编排【AI 辅助】（先设计后填空）；基础设施【AI 生成】。时间紧到只能手写一个文件，那就是 `agent/loop.py`。

### 卡壳时怎么跟 AI 说话

**【手写】**
```
「按脚手架教练模式带我写 agent/loop.py。先拆步，每步只给签名和测试，等我贴代码再下一步。」
「这一步卡死了……先提示思路，别给完整函数。」
「看参考实现吧，附理解题；我看完会自己重写。」
```

**【AI 辅助】**
```
「按 AI 辅助两阶段做 storage/cache.py：先讲清职责和 CacheBackend 接口草案，我确认后再填实现。」
「接口草案里 ttl 要不要必填？讲一下取舍，我再定。」
「契约确认了，可以填 Redis / SQLite 实现了。」
```

---

## 1. MVP 定义与验收标准

跑通以下单条命令流即算 MVP 完成：

```bash
docker compose up -d                          # 起 Redis
repo2resume init                              # 配置 API key、个人信息
repo2resume chat                              # 对话中完成：分析 → 荐职 → 生成 → 修改 → 导出
```

**全局验收标准（Definition of Done）：**

- [ ] 对 3 个以上真实本地仓库（含 1 个多人协作仓库用 `--author` 过滤）生成技能画像，画像中语言/领域占比与实际相符
- [ ] 基于画像推荐至少 5 个真实职位，排序合理（后端为主的画像不应把前端职位排第一）
- [ ] 针对选定职位生成简历，**每条 bullet 可溯源**到具体仓库/commit，无编造数字
- [ ] 在 `chat` 中用自然语言完成至少一轮修改（如"第二条经历改得更量化一点"）并生效
- [ ] 导出的 Markdown 结构完整（个人信息/技能/项目经历分节清晰），中英文正常，可直接粘贴到任意简历平台或用 pandoc 自行转换
- [ ] 全流程 LLM 成本单次 < $0.5（缓存命中后重跑 < $0.1）
- [ ] `evals run` 能输出简历质量评分报告

**明确不做（Out of Scope）：**

- Web 仪表盘（二期）、PDF 排版导出（二期，可先用 pandoc 手动转）、自动投递、爬 LinkedIn、tree-sitter 深度代码解析、embedding 微调、简历多模板

---

## 2. 阶段 A：Skill 原型（约 2 天）

**目的**：验证流程、沉淀提示词、积累评测数据。产出直接被阶段 B 复用。

### Phase A1 — 搭建 skill（1 天）

- [ ] **【AI 辅助】** `skill/scripts/git_stats.py`：遍历仓库、`--author` 过滤、输出统计 JSON（语言分布、commit 主题、活跃时间线、依赖清单）
- [ ] **【AI 生成】** `skill/templates/resume.md.j2`：简历 Markdown 模板
- [ ] **【手写】** `skill/prompts/`：四组核心提示词初稿（画像汇总 / 职位搜索与打分 / 亮点挑选 + bullet 写作 / 审稿 rubric）——提示词是你的领域知识，必须亲手迭代
- [ ] **【手写】** `skill/SKILL.md`：主流程编排 + 事实清单防幻觉规则 + HITL 确认点

**验收**：在 Cursor/Claude Code 中说"帮我根据 ~/code 下的项目生成一份简历"，宿主 agent 能按 SKILL.md 走完全流程。

### Phase A2 — 真实数据迭代（0.5-1 天）

- [ ] **【手写】** 用自己的真实仓库跑全流程 ≥ 3 轮（人工流程）
- [ ] **【手写】** 每轮记录失败案例并修改提示词（幻觉 / 亮点选错 / 语气问题分类记录）
- [ ] **【AI 生成】** 归档所有输入输出到 `skill/runs/`：git 统计 JSON、JD、生成的简历、修改意见 → 阶段 B 评测集种子
- [ ] **【手写】** 写一页结论：哪个环节质量最弱、提示词的关键教训

**Gate（继续阶段 B 的前提）**：生成的简历经人工审阅"有 70 分水平"——如果连宿主 agent 全力跑都不及格，先修流程再写 CLI。

---

## 3. 阶段 B：独立 CLI（约 16-25 天）

### Phase 0 — 项目骨架（1-2 天）

- [ ] **【AI 生成】** `pyproject.toml`（Python 3.11+，src 布局，`repo2resume` 入口点）
- [ ] **【AI 生成】** Typer CLI 骨架：`init / analyze / jobs / resume / chat / export / evals` 空命令
- [ ] **【AI 辅助】** `config.py`：`~/.repo2resume/config.toml` + 环境变量（API key、模型、个人信息）
- [ ] **【AI 辅助】** LiteLLM 封装：统一 `complete()` / `embed()`，含重试与超时
- [ ] **【AI 辅助】** `docker-compose.yml` 起 Redis；`storage/cache.py` 实现 `CacheBackend`（Redis 主 + SQLite 降级）——接口自己设计，实现让 AI 填
- [ ] **【AI 生成】** `storage/db.py`：SQLite 初始化与迁移
- [ ] **【AI 生成】** pytest 骨架 + ruff + CI（GitHub Actions 跑 lint/test）

**验收**：`repo2resume init` 完成配置向导；`pytest` 绿；Redis 停掉后 CLI 仍能跑（降级生效）。

### Phase 1 — 仓库分析（2-3 天）

- [ ] **【AI 辅助】** `analysis/git_miner.py`：PyDriller 遍历、`--author` 过滤、统计聚合（移植 A1 的 `git_stats.py`）
- [ ] **【AI 生成】** `analysis/tech_detector.py`：依赖文件/扩展名 → 技术栈（纯规则，不用 LLM）
- [ ] **【AI 辅助】** `storage/models.py`：`SkillProfile` / `ProjectSummary` 等 Pydantic 模型——schema 自己定
- [ ] **【AI 辅助】** `analysis/profiler.py`：LLM 结构化输出画像（提示词来自阶段 A），校验失败自动重试 ≤ 2 次
- [ ] **【手写】** 事实清单（fact sheet）机制：画像中每个论断挂载数据来源——防幻觉核心
- [ ] **【AI 生成】** 分析缓存：`analysis:{repo_hash}:{head_commit}`
- [ ] **【AI 生成】** 单测：对固定 fixture 仓库断言统计数字精确正确

**验收**：`repo2resume analyze ~/code/* --author me@example.com` 输出画像，Rich 表格展示语言/领域占比；重跑命中缓存秒回。

### Phase 2 — Harness 核心（3-4 天）★ 本项目技术核心

- [ ] **【手写】** `agent/tools.py`：Tool 抽象（名称/描述/Pydantic 参数 schema 自动转 JSON Schema）+ 注册表
- [ ] **【手写】★** `agent/loop.py`：手写 tool-use 循环（LLM 决策 → 执行工具 → 回填 → 循环；最大轮数;停止条件）——全项目最该亲手写的文件
- [ ] **【手写】** `agent/context.py`：会话历史、token 预算统计、超限时旧轮次压缩（compaction）
- [ ] **【手写】** Prompt Assembler：系统提示词动态组装（基础指令 + 当前状态摘要 + 工具说明）
- [ ] **【AI 辅助】** 会话持久化：`chat` 退出可恢复（SQLite）
- [ ] **【AI 辅助】** 把 Phase 1 的分析功能包装成工具接入 loop
- [ ] **【AI 辅助】** 单测：mock LLM 响应，断言循环的分支行为（直接回复/单工具/多轮工具/超限）

**验收**：`repo2resume chat` 中说"分析一下 ~/code/foo，我主要做了什么"，agent 自主调工具并给出正确回答；关掉重开能续聊。

### Phase 2.5 — Harness 加固（2-3 天）

- [ ] **【手写】** Lifecycle hooks：pre-tool-call（参数校验）/ post-tool-call（结果裁剪、事实清单核对）
- [ ] **【手写】** 权限分级：只读工具自动放行；写文件/联网工具需用户确认（可配置）
- [ ] **【AI 辅助】** Observability：每轮调用 trace 落 SQLite（prompt、响应、工具、token、耗时）；`chat` 里 `/cost` 查看累计成本
- [ ] **【手写】** 错误恢复：工具异常回传给 LLM 重试、结构化输出解析失败修复重问、重复调用同一工具的卡死检测
- [ ] **【AI 辅助】** 单测：注入故障（工具抛异常 / LLM 返回坏 JSON），断言恢复行为

**验收**：故意断网跑 `chat`，agent 报错但不崩溃并能继续；`/cost` 数字与 LiteLLM 计费一致。

### Phase 3 — 检索层 + 职位（3-4 天）

- [ ] **【AI 生成】** `retrieval/embedder.py`：LiteLLM embedding（默认 text-embedding-3-small，可切 bge-m3 本地）
- [ ] **【AI 辅助】** `retrieval/store.py`：ChromaDB 向量库 + SQLite FTS5 索引，项目/commit 组摘要入库
- [ ] **【手写】** `retrieval/hybrid.py`：BM25 + 向量双路召回 → RRF 融合——就几行，是 IR 知识落点
- [ ] **【手写】** `retrieval/rerank.py`：LLM rerank 先行（简单），cross-encoder `bge-reranker` 作对照实现——理解双塔 vs 交叉编码
- [ ] **【AI 辅助】** `jobs/search.py`：Tavily 搜索 + JD 抓取 + 24h Redis 缓存；支持手动粘贴 JD
- [ ] **【AI 辅助】** `jobs/matcher.py`：打分 = 向量相似度信号 + LLM 结构化评分
- [ ] **【手写】** 检索评测：用阶段 A 数据标注 10+ 条"JD-项目"相关度，算 Recall@5 / MRR
- [ ] **【AI 辅助】** 全部包装为工具接入 chat

**验收**：`repo2resume jobs` 推荐排序合理；给定 JD 召回的 top5 项目素材人工核对相关；Recall@5 ≥ 0.8。

### Phase 4 — 简历生成 + 多智能体（3-4 天）

- [ ] **【AI 辅助】** `resume/writer.py`：检索素材 → 亮点挑选 → bullet 生成（带溯源引用）
- [ ] **【AI 辅助】** `resume/critic.py`：事实核查 + ATS 建议，Writer-Critic 循环 ≤ 2 轮
- [ ] **【手写】** `agent/subagent.py`：子 agent 即工具；把 Repo Analyst / Job Scout 重构为独立上下文的子 agent——多智能体编排核心
- [ ] **【AI 生成】** `resume/render.py`：Jinja2 模板渲染，写出 Markdown 文件
- [ ] **【AI 生成】** `export` 命令 + chat 内导出
- [ ] **【AI 辅助】** 程序化断言：bullet 数量/长度、溯源存在性、禁用词

**验收**：MVP 全局验收标准第 3-5 条通过；对比"子 agent 拆分前后"同一任务的 token 消耗与质量，写一段结论（这就是你的多智能体学习成果）。

### Phase 5 — 评测与调优（2-3 天）

- [ ] **【手写】** `evals/datasets/`：整理阶段 A + 开发期积累的 golden 数据
- [ ] **【AI 辅助】** `evals/runner.py`：程序化断言 + LLM-as-judge（rubric 打分）
- [ ] **【AI 辅助】** `repo2resume evals run` 输出与上一版的对比表
- [ ] **【手写】** 据评测结果做 ≥ 2 轮提示词调优，每轮记录动机与得分变化
- [ ] **【AI 生成】** README：安装、快速开始、架构图、演示 GIF

**验收**：MVP 全局验收标准全部勾完；评测报告显示调优后分数提升。

---

## 4. 里程碑一览

| 里程碑 | 时点 | 可演示什么 |
|---|---|---|
| M1 | 阶段 A 结束（~第 2 天） | 宿主 agent 跑通全流程，简历质量 ≥ 70 分 |
| M2 | Phase 1 结束（~第 5 天） | CLI 能分析仓库出画像 |
| M3 | Phase 2.5 结束（~第 12 天） | 自研 harness 的对话 agent，可观测、可恢复 |
| M4 | Phase 3 结束（~第 16 天) | 职位推荐 + 混合检索，有检索指标 |
| M5 | Phase 4 结束（~第 20 天） | **完整 MVP**：闭环 + Markdown 导出 |
| M6 | Phase 5 结束（~第 23 天） | 评测体系 + 调优记录 + 可发布的 README |

## 5. 依赖与准备事项

- [ ] API key：一个主力 LLM（建议 Anthropic 或 OpenAI）+ Tavily（免费额度够 MVP）
- [ ] 本机装好 Docker Desktop
- [ ] 选定 3-5 个自己的仓库作为贯穿全程的测试数据（至少 1 个多人协作仓库）
## 6. 风险应对（执行层面）

| 风险 | 信号 | 应对 |
|---|---|---|
| 阶段 A 简历质量不及格 | Gate 不通过 | 停下修提示词/流程，不带病进入阶段 B |
| Phase 2 harness 复杂度失控 | 超 5 天仍不稳定 | 砍掉 compaction 等高级特性，先保最小循环可用，Phase 2.5 再补 |
| 职位搜索质量差 | Tavily 拿不到像样 JD | 降级为"用户粘贴 JD"为主、搜索为辅，不阻塞主线 |
| 时间超预算 | 任一 Phase 超估 50% | 优先保 M5（闭环),Phase 5 可压缩为只做程序化断言 |
