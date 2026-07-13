# Phase A1 实践复盘：Cursor Skill 原型（面试复习稿）

> 项目：Repo2Resume  
> 阶段：Phase A1 — 搭建 Skill 原型（`SKILL.md` + `git_stats.py` + 提示词骨架）  
> 用途：求职面试时复习「你在 Agent 项目里实际做过什么」+ **AI Agent 岗高频概念题**  
> 对应文档：`docs/design_docs/DESIGN.md` §0、`docs/design_docs/MVP_PLAN.md` §2 Phase A1  
> 核心增补：§6 面试问答（Skill / Harness / 幻觉 / RAG / 多智能体 / 评测）

---

## 1. 一句话项目定位（电梯陈述）

Repo2Resume 用 **本地 git 贡献** 生成可溯源简历，并按技能画像匹配职位。阶段 A 不先写 CLI，而是做成 **Cursor Skill**：宿主 Agent 提供大脑与工具，我们只沉淀领域流程与提示词，用来验证「代码 → 画像 → 职位 → 简历」是否真能产出可用简历。

面试可说：

> 我先用 Skill 验证简历生成闭环和防幻觉规则，再把验证过的提示词固化成独立 CLI agent（手写 tool-use loop）。这样避免一上来造 harness 却发现产品流程本身不成立。

---

## 2. 当前已完成什么（对照 MVP 计划）

| Phase A1 任务 | 状态 | 说明 |
|---|---|---|
| `skill/scripts/git_stats.py`（【AI 辅助】） | ✅ 已完成 | 多仓库、`--author`、语言/依赖/commit 主题统计 JSON |
| `skill/templates/resume.md.j2`（【AI 生成】） | ✅ 已完成 | Markdown 简历模板 |
| `skill/SKILL.md`（【手写】） | ✅ 已定稿清洗 | 五步流程 + 三处 HITL + 事实清单 + 归档 |
| `skill/prompts/` 四组提示词（【手写】） | 🔄 进行中 | `handwrite/prompts/` 脚手架已备，待亲手填写 |
| Cursor 注册 | ✅ | `.cursor/skills/repo2resume` → `skill/` |
| 设计文档 | ✅ | `DESIGN.md` / `MVP_PLAN.md` |
| Phase A2（真实仓库 ≥3 轮迭代） | ⏳ 未开始 | 等 prompts 手写完成后跑全流程 |

**阶段 A 的三个目标里，已推进：**

1. **流程验证**：主路径已写进 `SKILL.md`（分析 → 画像 → 搜岗 → 写简历 → 导出）。  
2. **提示词沉淀**：`SKILL.md` 已沉淀；四组 prompts 待手写。  
3. **数据积累**：归档规范已定（`runs/` + `meta.json`），跑通后才会有真实数据。

---

## 3. 做得好的地方（面试可当「亮点」讲）

### 3.1 混合架构：Skill 先于 CLI

- **做法**：Agent = Model + Harness；阶段 A 复用 Cursor 成品 harness，只写 Skill。  
- **为什么好**：用最低成本验证业务闭环；阶段 B 再手写 loop / RAG / 多智能体，并有对照组。  
- **面试话术**：先验证问题是否值得做，再投资框架层工程。

### 3.2 事实清单（Fact Sheet）防幻觉

- **做法**：贡献数字以 `stats.json` 为准；口述只补缺；冲突必问；bullet 强制 `<!-- src -->`。  
- **为什么好**：简历场景最大风险是编造；用「可溯源」把 LLM 输出变成可审计产物。  
- **面试话术**：不是靠模型「诚实」，而是用确定性规则约束概率输出——这是 harness engineering 思想在产品层的提前落地。

### 3.3 HITL 设计清晰

- **三处暂停**：画像确认 → 选定职位 → 定稿导出。  
- **Step 2 方向探索 vs Step 3 真实搜岗**：先给概括 title 扫盲，再搜 ≥20 取 top5，避免两个步骤抢职责。  
- **面试话术**：人机协同不是「全程聊天」，而是在高风险决策点设门禁。

### 3.4 编码方式分层（手写 / AI 辅助 / AI 生成）

- **做法**：`MVP_PLAN` 标注哪些必须亲手写（提示词、loop），哪些可 AI 填实现。  
- **为什么好**：把「学习 Agent」和「交付速度」拆开，避免全盘 AI 生成却说不清原理。

### 3.5 工程细节

- `git_stats.py` 按 author 过滤、排除 lock/node_modules、多邮箱 OR 语义。  
- `test_repos/`、`runs/`、`.DS_Store` 进 `.gitignore`。  
- PR 自动 description 工作流（开 PR 写 body；后续 push 按 trivial/substantive 决定是否重写）。

---

## 4. 踩过的坑（面试「失败经历 / 反思」很好用）

### 坑 1：把脚手架【】当成定稿

- **现象**：文件里仍是「【问用户】…怎么做？」，以为写了很多，实际 Agent 读到的是出题不是指令。  
- **教训**：Skill/Prompt 的读者是模型；必须是祈使句与可执行规则。  
- **面试升华**：提示词工程的交付物是「可执行规范」，不是给自己的草稿备忘。

### 坑 2：`description` 写成操作手册

- **现象**：frontmatter 里塞 WHAT=、WHEN= 填写说明和 1.2.3.4 流程。  
- **教训**：`description` 只服务 **技能发现**（第三人称 WHAT + 触发 WHEN）；流程放正文。  
- **对应知识**：Cursor Skill 的 progressive disclosure / 元数据设计。

### 坑 3：事实来源写反

- **现象**：一度写成「以用户口述为准」。  
- **风险**：无仓库证据也可写「精通 X」。  
- **纠正**：统计为准，口述补缺，冲突必问。  
- **面试升华**：RAG/Agent 里「权威数据源」设计比模型措辞更重要。

### 坑 4：阶段 A 异常处理抄成「通用 Agent 教材」

- **现象**：写 GitHub API 超时、绕过权限、fallback clone。  
- **教训**：阶段 A 只有本地 `git_stats.py`；异常应对齐真实工具面（`errors` / `author_commits==0`）。  
- **面试升华**：错误恢复策略必须匹配实际工具集，否则是空话。

### 坑 5：Step 边界不清

- **现象**：Step 2 若直接搜真实 JD，会与 Step 3 重复。  
- **解决**：Step 2 只给概括职位类型（不联网）；Step 3 再搜真实岗位。  
- **面试升华**：多步骤 Agent 工作流要做 **职责单一化**，否则上下文互相污染。

### 坑 6：符号链接与双路径困惑

- **现象**：`.cursor/skills/repo2resume/...` 与 `skill/...` 看起来像两份文件。  
- **实际**：symlink，同一 inode。  
- **教训**：项目级 skill 放仓库 `skill/`，用链接注册到 Cursor 约定目录。

---

## 5. 本阶段锻炼的知识点（按面试专题整理）

### 5.1 Agent / Skill 原型

| 知识点 | 你怎么练到的 |
|---|---|
| Agent = Model + Harness | 阶段 A 用宿主 harness；阶段 B 计划自研 |
| Skill 作为领域说明书 | `SKILL.md` 编排五步，不实现 loop |
| HITL（Human-in-the-loop） | 三处 ⏸ 门禁 |
| 子流程拆分 | 画像 / 搜岗 / 写作 / 审稿 |

**可能被问：** Skill 和 Agent 有什么区别？  
**答：** Skill 是给已有 Agent 的领域指令与工具约定；Agent 还包含 loop、工具执行、权限与状态。我们阶段 A 只写 Skill，阶段 B 造 Agent 运行时。

### 5.2 提示词工程（Prompt Engineering）

| 知识点 | 落点 |
|---|---|
| 系统级流程提示 vs 环节提示 | `SKILL.md` vs `prompts/01–04` |
| 结构化输出 | 画像/打分要求 JSON schema（prompts 待填） |
| 防幻觉 / 证据锚定 | 事实清单、`<!-- src -->` |
| Few-shot 好坏对照 | 计划在 `03_resume_writing` |
| Writer–Critic 分离 | Step 4 自查 + 最多 2 轮 |

**可能被问：** 如何降低简历幻觉？  
**答：** 锁定 fact sheet（git 统计）、强制溯源注释、冲突必问、审稿一票否决无出处数字——不靠「请不要编造」一句话。

### 5.3 上下文与信息密度

- 不把整仓源码塞进模型；先用脚本压缩成统计 JSON。  
- **面试点：** Context engineering / 信息预算——送模型的应是高密度事实，不是原始噪声。

### 5.4 工程与协作

- Git ignore、PR 描述自动化、编码方式标注（手写/AI 辅助）。  
- **面试点：** 能讲清「哪些必须自己写才能学到 Agent 内核」。

### 5.5 尚未在本阶段深挖（诚实边界）

以下在 DESIGN 里，但 **要到阶段 B** 才真正实现——面试被问到要说「设计过、阶段 A 有意不做」：

- 手写 tool-use / ReAct 循环、hooks、权限、trace  
- Embedding、混合检索、Rerank  
- 多智能体（子 agent 即工具、Writer-Critic 运行时）  
- 评测集 / LLM-as-judge 自动化  

阶段 A 的价值是：**带着「见过单 Agent 硬扛」的体感**，再去做这些。

---

## 6. AI Agent 开发岗：高频面试问答（结合本项目）

下面按「概念 → 简答 → 结合 Repo2Resume 怎么答」组织，方便突击复习。

### 6.1 Skill / Agent / Prompt / Tool 概念题

**Q1：什么是 Skill？和 Prompt 有什么区别？**

- **简答：** Prompt 通常是一次或一轮对话里的指令文本。Skill 是一套**可复用的领域能力包**：常含 `SKILL.md`（何时用、怎么做）、可选脚本/模板/子提示词；由宿主 Agent 在匹配到场景时加载执行。  
- **结合项目：** `repo2resume` Skill = 流程说明书 + `git_stats.py` + `prompts/` + 简历模板；不是单次「帮我写简历」的聊天记录。

**Q2：Skill 和 Agent 有什么区别？**

- **简答：** Agent = **Model + Harness**（循环、工具调用、权限、上下文、状态）。Skill 是给 Agent 用的**领域插件/说明书**，一般不自己实现 loop。  
- **结合项目：** 阶段 A 只写 Skill，跑在 Cursor 的 harness 上；阶段 B 自己实现 harness，把同一套领域流程固化进 CLI。

**Q3：Skill 的设计逻辑一般是什么？（开放题，按层次答）**

可按四层说：

1. **发现层（description）：** 第三人称写清 WHAT + WHEN，让路由/宿主决定何时加载。  
2. **编排层（SKILL 正文）：** 步骤、分支、暂停点（HITL）、失败怎么处理。  
3. **能力层（tools/scripts）：** 确定性工作交给脚本（如 git 统计），LLM 做归纳与写作。  
4. **约束层（事实清单/权限）：** 防幻觉、防越权；输出格式可被下游消费。

**结合项目：** description 触发「生成简历」→ 五步编排 → `git_stats.py` 出事实 → 事实清单约束写作。

**Q4：什么是 Tool？和 Skill 什么关系？**

- **简答：** Tool 是模型可调用的**单个能力**（函数/命令/API），有 schema。Skill 是**多步工作流 + 规范**，执行中会调用多个 tool（读文件、跑脚本、联网搜索）。  
- **结合项目：** `python3 scripts/git_stats.py` 是工具调用；整份 `SKILL.md` 是 Skill。

**Q5：MCP 是什么？和 Skill 啥关系？（了解即可）**

- **简答：** MCP（Model Context Protocol）是 Agent 连接外部工具/数据源的一种协议标准。Skill 偏「领域流程与提示」；MCP 偏「标准化工具接入」。可同时存在：Skill 规定何时用哪类工具，工具可由 MCP server 提供。

---

### 6.2 Harness / Loop / 框架题

**Q6：什么是 Agent Harness？为什么说 Agent = Model + Harness？**

- **简答：** 模型只做下一 token 预测；Harness 提供 tool-use 循环、上下文管理、权限、hooks、持久化、可观测性。没有 harness，模型不能真正「行动」。  
- **结合项目：** 阶段 A 用 Cursor 当成品 harness；阶段 B 计划手写 loop（学习目标）。

**Q7：什么是 tool-use 循环？和 ReAct 什么关系？**

- **简答：** 循环是「模型决策 →（可选）调工具 → 观察结果 → 再决策」直到结束。ReAct（Reason+Act）是早期用文本 Thought/Action 格式实现的同构范式；现代多用原生 function calling，更稳。  
- **结合项目：** Skill 假设宿主会跑这个循环；我们不在阶段 A 实现它。

**Q8：为什么不用 LangChain，而要手写 loop？**

- **简答：** LangChain/LangGraph 是零件箱，适合快搭；手写能掌握 harness 原理，面试和排障更强。生产上两者都合理，看目标是「交付」还是「吃透」。  
- **结合项目：** 明确选型手写 + LiteLLM；框架作对照阅读，不当主依赖。

**Q9：Plan-and-Execute 和 ReAct 式逐步决策有何不同？**

- **简答：** Plan-and-Execute 先出完整计划再执行；ReAct 走一步看一步。交互多、要 HITL 的场景常用逐步决策；固定流水线可用预编排 pipeline。  
- **结合项目：** 用户对话路径偏逐步 + 门禁；Writer-Critic 偏固定小循环。

---

### 6.3 可靠性：幻觉、结构化输出、HITL

**Q10：Agent 幻觉怎么治理？**

答题框架（由硬到软）：

1. **缩小模型自由裁量：** 权威数据源（fact sheet）  
2. **可验证输出：** 引用/溯源、schema 校验  
3. **确定性挡板：** hooks、程序断言、禁止词  
4. **人审门禁：** HITL  
5. **最后才是**「请勿编造」类提示词  

**结合项目：** `stats.json` + `<!-- src -->` + 冲突必问 + Critic 一票否决。

**Q11：什么是 Structured Output？为什么重要？**

- **简答：** 强制模型按 JSON Schema / 类型输出，便于程序校验与编排。Agent 工程核心课：**LLM 输出必须可被程序消费**。  
- **结合项目：** 画像、职位打分要 JSON；失败则重试（阶段 B 还会做 Pydantic 校验）。

**Q12：什么是 HITL？什么时候必须上？**

- **简答：** Human-in-the-loop = 关键步骤暂停等人确认。适合：不可逆操作、对外发布内容、价值/合规判断、数据冲突。  
- **结合项目：** 画像确认、选定职位、定稿导出；不是每一步都问，避免噪声。

**Q13：权限与安全在 Agent 里怎么做？（阶段 B 也能答设计）**

- **简答：** 工具分级（只读自动、写入/联网需确认）、最小权限、沙箱、审计日志。  
- **结合项目：** Skill 层先写「不得擅自改用户仓库」；阶段 B 计划权限分级 + hooks。

---

### 6.4 上下文、记忆、RAG

**Q14：什么是 Context Engineering？**

- **简答：** 管理「每轮塞进模型的信息」：系统提示、工具说明、历史、检索结果；含截断、摘要（compaction）、按需加载，防止 context rot。  
- **结合项目：** 先用脚本把 git 压成统计 JSON，而不是灌全量 diff。

**Q15：RAG 是什么？你们项目怎么用？**

- **简答：** Retrieval-Augmented Generation：先检索再生成，用外部知识降低胡编。  
- **结合项目：** 阶段 B 用混合检索（BM25 + 向量 + RRF + rerank）从项目摘要里按 JD 召回亮点；阶段 A 用长上下文硬读当对照组。

**Q16：为什么要混合检索 + Rerank？**

- **简答：** 向量擅语义，BM25 擅关键词（如 Kubernetes）；RRF 融合；Rerank（cross-encoder）精排。  
- **结合项目：** JD 里精确技术词不能只靠 embedding。

---

### 6.5 多智能体与评测

**Q17：什么是多智能体？什么时候值得用？**

- **简答：** 多个独立系统提示/上下文/工具集的协作。收益是聚焦上下文、分工模型、提示词好维护；代价是贵、慢、难调。  
- **结合项目：** 阶段 B：Orchestrator + 分析/搜岗子 agent + Writer-Critic；阶段 A 单 Agent 一把梭做对照。

**Q18：子 agent 即工具是什么意思？**

- **简答：** 把另一个 agent 的完整小循环封装成主 agent 可调的一个 tool；主 agent 只拿摘要结果，子 agent 自有上下文。  
- **结合项目：** DESIGN §4 推荐实现方式。

**Q19：如何评测 Agent / 提示词？**

- **简答：** Golden dataset + 程序断言（格式、溯源存在）+ LLM-as-judge + 人工抽查；改 prompt 要回归。  
- **结合项目：** `runs/` 归档就是评测种子；阶段 B 做 `evals run`。

---

### 6.6 产品与架构设计题（常用来挖项目深度）

**Q20：为什么先做 Skill 再做 CLI？**

- **答：** 验证闭环与提示词质量；宿主已提供 loop/搜索/对话；失败成本低；通过后再造 harness，学习路径也更清晰。

**Q21：如何给多步骤 Agent 划分步骤？**

- **答：** 单一职责、显式输入输出、在「价值判断/对外输出」处设 HITL、能脚本化的不做 LLM。  
- **结合项目：** Step2 方向探索 vs Step3 真实搜岗的边界就是反例驱动设计出来的。

**Q22：事实清单（grounding）和 RAG 有何不同？**

- **答：** 事实清单是**锁定已抽取的权威字段**（常作硬约束）；RAG 是**按需检索片段**进上下文。可组合：先检索候选，再只允许基于 fact sheet 写数字。

**Q23：若简历仍在编造，你怎么排查？**

答题顺序：

1. 查统计是否缺字段 / author 过滤错误  
2. 查 prompt 是否允许「美化」  
3. 查是否缺少溯源校验  
4. 查 Critic 是否真跑、轮次是否被跳过  
5. 加程序断言拦截无 `src` 的 bullet  

---

### 6.7 易混概念速查表

| 概念 | 一句话 |
|---|---|
| Model | 推理引擎 |
| Harness | 让模型能循环调工具、管上下文与权限的运行时 |
| Agent | Model + Harness（+ 常含领域提示） |
| Skill | 领域工作流与规范，挂在现成 Agent 上 |
| Tool | 单次可调用能力 |
| Prompt | 文本指令（可嵌在 Skill 里） |
| HITL | 关键步骤人确认 |
| RAG | 先检索再生成 |
| Multi-agent | 多套提示/上下文/工具协作 |
| Eval | 用固定集回归提示词与行为质量 |

---

## 7. 若面试官追问「你个人贡献是什么」

建议诚实区分：

| 部分 | 角色 |
|---|---|
| 产品流程、HITL、事实清单、Step 2/3 边界 | **你主导设计并手写迭代** |
| `git_stats.py`、早期 prompt/模板草稿 | AI 辅助生成，你验收与纠偏（如 merge 口径、author 过滤） |
| 定稿清洗 `SKILL.md` | 在你手写稿基础上结构化润色，产品决策仍是你的 |

面试强调：**你能讲清为什么这样设计、踩过什么坑、如何验收**，而不是每一行都手打。

---

## 8. 下一步（A1 收尾 → A2）

1. 按脚手架手写 `handwrite/prompts/01–04`，定稿拷到 `skill/prompts/`。  
2. 用 `test_repos/` 跑通全流程 ≥1 次，再按 A2 做 ≥3 轮迭代。  
3. 归档 `runs/` + 写一页「哪一环最弱」结论 → 作为阶段 B 评测种子。

---

## 9. 30 秒速记卡

- **做了什么：** Cursor Skill 把「git → 画像 → 职位 → 可溯源简历」写成可执行流程。  
- **核心设计：** 事实清单 + HITL + Step2 方向探索 / Step3 真实搜岗。  
- **踩坑：** 脚手架当定稿、description 写错、口述覆盖统计、异常写成空中楼阁。  
- **学到什么：** Skill 原型、提示词分层、防幻觉、人机门禁、先验证再造 harness。  
- **必背定义：** Skill ≠ Agent；Agent = Model + Harness；用事实清单+溯源+HITL 治幻觉。  
- **下一步：** 手写四组 prompts → A2 真跑 → 阶段 B 手写 agent loop。
