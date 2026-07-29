# Phase B / Phase 4 实践复盘：简历生成 + 子 Agent + 贡献归属（面试复习稿）

> 项目：Repo2Resume  
> 阶段：Phase 4 — 简历生成（Writer-Critic）+ 多智能体（子 agent）+ E2E 可用性加固  
> 对应：`MVP_PLAN.md` Phase 4；前置 `04_phase_3_retrieval_jobs.md`  
> 验收日：2026-07-29（chat：分析 → 搜岗 → 选岗 → 生成 `resume_draft.md`；贡献归属收紧后主观质量可用）

## 1. 一句话定位

Phase 3 解决了「按 JD 找素材 / 推荐职位」，但还不会产出可投递的项目经历。Phase 4 把闭环接到「写出可溯源、少贪功的 Markdown 简历」：检索素材 → Writer 起草 → Critic 审稿（≤2 轮）→ Jinja 渲染落盘；并用子 agent 把「仓库分析」「搜岗」从主对话拆成独立上下文。面试可说：我做了同会话 Writer-Critic + 程序化硬过滤，又用 `author_share` 把「只写本人 commit」落成**选材规则**，而不是只靠提示词口头约束。

## 1.5 Phase 4 的完整流程

Phase 4 有两条线要讲清：**构建顺序**（模块怎么叠）和 **运行时数据流**（用户选岗之后代码怎么走）。另有一条「贡献归属」横切写作与选材。

### A. 构建 / 注册顺序（为什么按这个顺序写）

```
① resume/writer.py          —— select_materials + write_experience（结构化 JSON）
② resume/critic.py          —— programmatic_checks + LLM critique + revise
③ resume/pipeline.py        —— hybrid 检索 → Writer-Critic → 落库/渲染
④ resume/render.py          —— Jinja2 → Markdown（含 <!-- src --> 溯源注释）
⑤ agent/subagent.py 【手写】 —— SubAgentRunner：独立 Context + 工具子集
⑥ builtins / chat_ui/setup  —— repo_analyst / job_scout / generate_resume 接线
⑦ 贡献归属加固              —— fact_sheet_from_profile + caution 强制写入 + 低份额剔除
⑧ UX 加固                   —— 进度 tick、超时降级、并发搜岗、批量打分、Ctrl-C
```

**依赖直觉：** 没有 Writer/Critic 契约就没有 pipeline；没有 pipeline 就没有 `generate_resume` 工具；子 agent 依赖 Phase 2 loop + Phase 2.5 hooks，但不改主 loop 内核。贡献归属与 UX 是真跑逼出来的加固，挂在已有流水线上，而不是另起一套。

### B. 端到端用户流程（chat 主路径）

```
用户：「开始，我是 Rain」
  │
  └─ 主 Agent 调 repo_analyst（子 agent）
        ├─ 独立 Context + analyze_repo（作者过滤挖矿）
        ├─ tech_detector + fact_sheet + Profiler → SkillProfile
        ├─ _ensure_authorship_caution（author_share<0.15 强制 caution 行）
        └─ 写入 SQLite skill_profiles → 主会话汇报方向

用户：「搜职位」/「好」
  │
  └─ 主 Agent 调 job_scout（子 agent）
        ├─ search_jobs（Tavily→Bocha→mock；多关键词并发）
        ├─ JobMatcher：向量粗排 + top-N 一次批量 LLM 打分
        └─ 展示带内部 job_id 的列表

用户：「1」/「第 1 个」
  │
  └─ generate_resume(job_id=…)   [PermissionHook：写盘确认 y/N]
        ├─ 读最新 SkillProfile
        ├─ upsert_profile_materials（画像亮点；简历路径常不传全仓 README）
        ├─ hybrid_search(JD) → select_materials（低份额剔除/降权）
        ├─ fact_sheet_from_profile(profile) 注入 Writer/Critic
        ├─ Writer → Critic ≤2 → revise(must only)
        └─ render → resume_draft.md + DB resume draft

用户：「这些是不是我写的 / 不要贪功」
  │
  └─ 系统提示要求再次 generate_resume(同一 job_id)，收窄措辞重写
      （不要只让用户手改 Markdown）
```

### C. Writer-Critic 内部流水线（生成简历时）

```
materials (SearchHit[]) + SkillProfile + FactSheet
        │
        ▼
  write_experience (LLM → ResumeDraft JSON)
        │
        ▼
  ┌──── critique 循环（最多 max_rounds=2）────┐
  │  1) programmatic_checks（禁词/空证据/贪功句）│
  │  2) LLM CritiqueReport（must / should）      │
  │  3) 合并：硬过滤 must 优先                     │
  │  若 approved → break                         │
  │  否则 revise_experience(must only) → 再审     │
  │  Critic 超时/坏 JSON → 降级硬检查，不整单炸    │
  └──────────────────────────────────────────────┘
        │
        ▼
  render_resume → Markdown（bullet 后 <!-- src: repo, detail -->）
```

**心智模型：** Writer 负责「写得出」；程序化检查负责「明显违规必杀」；LLM Critic 负责「模糊质量」；revise 只消化 must，避免 should 拉长链路。

### D. 子 Agent 与主 Loop 的关系

```
主 AgentLoop（chat）
  ├─ 工具：repo_analyst / job_scout / generate_resume …
  │         │
  │         ├─ repo_analyst / job_scout
  │         │     └─ SubAgentRunner.run(task)
  │         │           ├─ 新建 ContextManager（与主会话隔离）
  │         │           ├─ 工具子集（如只有 analyze_repo / search_jobs）
  │         │           ├─ 自带 ErrorRecoveryHook 等
  │         │           └─ 缩小版 loop → 把最终文本当 Tool 结果回填主会话
  │         │
  │         └─ generate_resume
  │               └─ run_resume_pipeline（同进程，无第二套 loop）
  │
  └─ 主 harness：Permission / Trace / ErrorRecovery / 卡死检测 仍包住外层 call
```

**两条别混：** Writer-Critic 是**同会话多角色提示**；Repo Analyst / Job Scout 才是**多智能体**（独立上下文 + 工具子集）。

### E. CLI 对照（不经过主对话时）

| 入口 | 行为 |
|------|------|
| `repo2resume chat` | 主路径：子 agent + `generate_resume` |
| 直接调 `run_resume_pipeline(...)` | 测试/脚本可用；需自备 profile、db、embedder、llm |
| 独立 `export` 命令 | ⚠️ 未作为完整产品命令验收（生成工具已写盘） |

### F. 数据落在哪

| 数据 | 位置 |
|------|------|
| SkillProfile（含 caution） | SQLite `skill_profiles` |
| 职位 JD / job_id | SQLite `jobs` |
| 简历草稿结构化 | SQLite resume draft 表（`save_resume_draft`） |
| 用户可见 Markdown | 默认 `resume_draft.md`（工作目录） |
| 检索素材索引 | ChromaDB + FTS5（画像 chunk） |
| 工具 trace /cost | `tool_traces`（Phase 2.5） |
| 低贡献事实 | caution 行 + `fact_sheet_from_profile` 运行时合成 |

### G. 本阶段刻意没接 / 未做硬 Gate 的

- 简历路径默认**跳过 LLM rerank**（延迟；hybrid 顺序够用）
- Writer-Critic **不拆**成两个子 agent（同 JD 短轮次，省 token）
- 独立多格式 `export` 命令未做完
- 「子 agent 拆分前后」token 对比书面结论未写（MVP 文案可补）
- Phase 5 评测套件不在本阶段 Gate 内

## 2. 本阶段完成了什么（对照计划）

| Step / MVP 项 | 交付物 | 状态 |
|---------------|--------|------|
| Writer：检索 → bullet（带溯源） | `resume/writer.py`、`prompts/resume/write_*.j2` | ✅ |
| Critic：事实核查 + 循环 ≤2 | `resume/critic.py`、`writer_critic_loop` | ✅ |
| 程序化断言 | `programmatic_checks`（禁词/空 evidence/贪功句） | ✅ |
| 子 agent：Repo Analyst / Job Scout | `agent/subagent.py` + chat 注册 | ✅ |
| Markdown 渲染 | `resume/render.py` → `resume_draft.md` | ✅ |
| export / chat 内导出 | 生成即写盘；独立 `export` 可再补 | ⚠️ 部分 |
| 贡献归属（只写本人 commit） | caution 强制 + 选材阈值 + 提示词 | ✅ 加固 |
| 真跑 E2E | chat 选岗生成；超时/贪功已迭代 | ✅ |

交付物路径：

- `src/repo2resume/resume/writer.py` / `critic.py` / `pipeline.py` / `render.py`
- `src/repo2resume/agent/subagent.py`
- `src/repo2resume/agent/builtins.py`（`generate_resume` 等）
- `src/repo2resume/chat_ui/setup.py`（系统提示含贡献归属 / 勿连打）
- `src/repo2resume/analysis/fact_sheet.py`（`authorship_hints_from_profile` / `fact_sheet_from_profile`）
- `src/repo2resume/analysis/pipeline.py`（`_ensure_authorship_caution`）
- `src/repo2resume/prompts/resume/write_experience.j2` / `critic_review.j2` / `revise_experience.j2`
- `src/repo2resume/prompts/analyzer/build_skill_profile.j2`（caution 格式）
- `tests/test_resume_writer.py` / `test_resume_critic.py` / `test_fact_sheet.py`（及相关 agent 测）

验收：chat 真跑「分析 → 搜岗 → 选 1 → 确认写盘」产出 `resume_draft.md`；低 `author_share` 仓默认剔除或模块级收窄；Writer/Critic/fact_sheet 相关单测绿。  
**未纳入本阶段硬 Gate：** Phase 5 evals、子 agent token 对比书面结论、完整 `export` CLI。

## 3. 做法 · 影响 · 亮点 · 踩坑

### 3.1 做了什么

- 实现 `select_materials` + `write_experience`：按仓去重、限项目数，LLM 输出带 `evidence` 的 `ResumeDraft`。
- 实现 `programmatic_checks` + `critique` + `revise_experience` + `writer_critic_loop`（≤2 轮，revise 只吃 must）。
- 实现 `run_resume_pipeline`：解析 JD/`job_id` → 索引画像素材 → hybrid → Writer-Critic → 落库渲染。
- 手写 `SubAgentRunner` / `SubAgentSpec`：主工具 `repo_analyst`、`job_scout` 委派独立上下文。
- 贡献归属：分析后强制 caution 行；生成时 `fact_sheet_from_profile`；`author_share < 0.12` 硬剔除；`0.12–0.15` 降权限条；正则拦贪功句；chat 提示质疑时重跑 `generate_resume`。
- E2E 加固：Critic 超时/坏 JSON 降级；跳过简历 LLM rerank；matcher 批量打分；搜岗并发；主线程 wait tick；工具返回「【已完成】」；空闲 Ctrl-C 两次退出。

### 3.2 对项目的影响

| 影响面 | 说明 |
|--------|------|
| 产品行为 | chat 闭环到可编辑的 Markdown 草稿；低贡献仓不再默认整页贪功 |
| 提示词 | Writer/Critic/Profiler 明确模块级措辞与可解析 caution 格式 |
| 数据契约 | caution 从「LLM 随缘」变为「统计强制 + 可解析」；写作链路不断档 |
| Harness | 长链路下权限/超时/进度/卡死被真实压力测过，Phase 5 有失败样例可回归 |
| 与 Phase 3 关系 | RAG 负责「找得到」；Phase 4 负责「写得诚实」——检索命中 ≠ 个人主导 |

### 3.3 亮点（可上面试）

1. **硬过滤 + LLM 审稿分层**：禁词、空 evidence、贪功句正则必杀；风格/ATS 交给 Critic——可判定规则不交给概率模型。
2. **贡献份额进选材，不只进 prompt**：`select_materials` 按 `author_share` 剔除/降权，比「请注意 caution」可靠一个数量级。
3. **子 agent = Tool**：对外同构普通工具，主 loop 无特判；对内缩小版 loop + 工具子集，隔离上下文降 tool-spam。
4. **超时降级优于重试爆炸**：Critic 挂了仍落盘草稿 + 明确「勿再调用」，避免 Air 连打写盘工具。

### 3.4 踩坑

**坑 1：挖矿过滤 ≠ 简历诚实**  
- 现象：`--author` 已过滤 commit，bullet 仍写「设计并实现完整系统 / 全部前后端」。  
- 原因：素材来自仓级摘要/亮点；Writer 把「仓库能做什么」写成「我主导了什么」。  
- 教训：溯源注释防的是假来源；贪功要靠份额选材 + 措辞规则 + 硬过滤。  
- 面试升华：Attribution 是独立问题，不是 RAG 的免费附赠。

**坑 2：fact_sheet 断链**  
- 现象：`write_experience` / `critique` 参数有 `fact_sheet`，pipeline 长期传 `None`，prompt 里永远 `(empty)`。  
- 原因：接口预留了，生成路径没接线；简历阶段又往往没有完整 `RepoStatsBundle`。  
- 教训：接口有 ≠ 数据流通；用 `fact_sheet_from_profile` 从 caution 合成，并在 analyze 强制写入可解析 caution。  
- 面试升华：反幻觉清单必须出现在**最终调用**的 prompt 里，画架构图不够。

**坑 3：Critic 坏 JSON × 超时**  
- 现象：第 2 轮审稿 JSON 非法 → 重试 → LLM 超过 120s → 整单 `generate_resume` 失败。  
- 原因：结构化输出不稳定 + 超时未在 Critic 边界捕获。  
- 教训：schema 重试限次；`TimeoutError` catch 后回退硬检查，保证草稿可落盘。  
- 面试升华：Agent 产品化优先「可继续 / 可解释」，而不是「必须完美通过审稿」。

**坑 4：进度假死 / 死锁**  
- 现象：长时间「处理中」无阶段感；或 Rich Live + 后台心跳线程卡住终端。  
- 原因：UI 刷新与后台线程争用；阶段标签未绑定到当前 LLM 等待。  
- 教训：wait tick 放主线程；阶段名用 ContextVar；少用 Live+多线程拼进度。  
- 面试升华：可观测性包含「用户是否知道卡在哪」，不只 `/cost` 数字。

**坑 5：匹配串行超时**  
- 现象：搜岗后对十来个职位逐个 LLM 打分，耗时数分钟。  
- 原因：N 次串行 complete，每次还可能触达 timeout。  
- 教训：向量粗排全量 → top-N **一次批量 LLM**；失败回退向量分。  
- 面试升华：延迟预算决定精排形态；批量是工程杠杆。

**坑 6：成功后仍重跑 generate_resume**  
- 现象：草稿已写入，模型因 `approved=False` / must>0 再次调写盘工具。  
- 原因：工具返回像「未完成」；系统提示未禁止连打。  
- 教训：返回写死「【已完成】勿再调用」；系统提示同步；超时失败也不要自动连打。  
- 面试升华：工具契约是给模型看的 API，成功语义必须显式。

**坑 7：子 agent 参数类型**  
- 现象：`authors='["Rain"]'` 以字符串传入 → Pydantic 校验失败 → 子 agent 无 ErrorRecovery 时外层反复重试像卡死。  
- 原因：LLM 常把 list 参数 JSON 字符串化；子 registry  initially 缺恢复钩子。  
- 教训：参数 coerce（JSON 字符串→list）+ 子 registry 挂 ErrorRecovery + 提示「同一请求只调一次」。  
- 面试升华：多 agent 时，**每个** registry 都要具备与主链路同级的恢复策略。

## 4. 本阶段知识点（面试向）

每个技术点按**同一套五问**答（临场可直接套）：

1. **X 是什么？**
2. **为什么要在 Repo2Resume（这个项目）里用它？**
3. **为什么不用……（更偷懒 / 更常见的替代做法）？**
4. **有什么优缺点？**
5. **为什么不用其他同类技术 / 竞品？**

| 技术点 | 本项目落点 |
|--------|------------|
| Writer-Critic | `writer_critic_loop`，同会话 ≤2 轮，revise 只吃 must |
| 程序化断言 vs LLM 审稿 | `programmatic_checks` 先跑，再 LLM `CritiqueReport` |
| 子 Agent（多智能体） | `SubAgentRunner`：`repo_analyst` / `job_scout` |
| 贡献归属 / Attribution | `author_share` caution + 选材阈值 + 贪功正则 |
| Fact sheet（写作链路） | `fact_sheet_from_profile` + `_ensure_authorship_caution` |
| 超时与降级 | Critic/LLM timeout → 硬检查 / 向量分；勿连打工具 |
| 粗排 / 精排预算 | hybrid 粗排；简历路径跳过 LLM rerank |

---

### ① Writer-Critic（同会话生成-审稿循环）

**1. 是什么？**  
先让「作者」模型按素材生成结构化草稿，再让「审稿」模型（或同一模型换角色提示）按 rubric 挑 must/should，再修订。本项目限制 ≤2 轮，且 revise 默认只处理 must。

**2. 为什么要在这个项目用它？**  
简历有硬约束（可溯源、禁免责声明数字、勿贪功）又有软约束（ATS、密度 STAR）。单次生成很难一次过；循环把「写」和「查」分开，失败可修复。

**3. 为什么不用「一次 prompt 要求模型自检后直接输出终稿」？**  
自检易流于形式；must 与 should 混在一次输出里难控；也无法插入程序化硬过滤。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 约束可迭代满足 | 多轮 LLM，延迟与费用上升 |
| 角色分离，prompt 更清晰 | 坏 JSON / 超时会放大失败面（需降级） |
| 与硬过滤天然组合 | 轮次要封顶，否则抖修订 |

**5. 为什么不用其他同类技术？**  
- **拆成两个子 agent：** 同 JD/同素材短修订，隔离上下文收益小、token 更贵 → 本项目不用。  
- **只靠人工改 Markdown：** MVP 要自动化闭环；人工是兜底不是主路径。  
- **强化学习 / 训练审稿模型：** 过重，非学习目标。

---

### ② 程序化断言 vs LLM-as-judge

**1. 是什么？**  
对草稿做确定性检查（正则/空字段/结构），产出 must；LLM 再做模糊质量判断。两者合并时硬过滤优先。

**2. 为什么要在这个项目用它？**  
「正文出现 author_share」「evidence.source 为空」「设计并实现完整…」是明确违规，不该赌模型每次都抓到。

**3. 为什么不用「全部交给 Critic LLM」？**  
不稳定、贵、且会与 JSON 解析失败耦合；回归测试也难写。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 可单测、可回归 | 规则要维护，覆盖不全 |
| 零 token、毫秒级 | 过严可能误杀合理措辞 |
| 给 LLM 减负 | 表达多样性靠规则很难穷尽 |

**5. 为什么不用其他同类技术？**  
- **JSON Schema 只校验结构：** 管不了「贪功语义」。  
- **纯启发式打分器：** 可做补充，但替换不了带 evidence 的语义审稿。

---

### ③ 子 Agent（多智能体编排）

**1. 是什么？**  
对外暴露为一个 Tool；对内新建 Context + 工具子集 + 缩小版 loop，跑完把文本结果回填父 agent。本项目：`repo_analyst`、`job_scout`。

**2. 为什么要在这个项目用它？**  
分析与搜岗目标不同、工具不同、对话易污染主会话（错误参数反复重试）。隔离上下文降低 tool-spam，主助手只做编排与对用户说话。

**3. 为什么不用「主 loop 直接挂 analyze_repo / search_jobs，靠系统提示分角色」？**  
多角色提示 ≠ 多智能体：历史与工具仍共享，分析失败容易拖死主会话；提示词也无法真正限制「只能用搜岗工具」。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 上下文隔离、职责清晰 | 多一跳 LLM，延迟与费用增加 |
| 与主 Tool 同构，loop 无特判 | 每个子 registry 都要挂恢复/权限策略 |
| 便于单独测「分析专家」「搜岗专家」 | 参数契约（list vs JSON 字符串）更易踩坑 |

**5. 为什么不用其他同类技术？**  
- **LangGraph / Crew 类框架：** 能做，但本项目目标是吃透手写 loop；自研 `SubAgentRunner` 更贴学习目标。  
- **进程级多服务：** MVP 过重。  
- **把 Writer-Critic 也拆子 agent：** 见①，短轮次同素材不划算。

---

### ④ 贡献归属（Attribution）与 author_share

**1. 是什么？**  
保证简历声称的工作能对应到**用户本人提交**，并在团队仓用份额约束措辞与是否入选。本项目：挖矿 author 过滤 + caution + 选材阈值（&lt;0.12 剔除，&lt;0.15 限写）+ 贪功句硬过滤。

**2. 为什么要在这个项目用它？**  
真实用户反馈：「质量不错，但有些功能不是我写的。」课程/开源团队仓占比低时，RAG 仍可能召到仓级亮点，模型会写成整站主导。

**3. 为什么不用「只在 prompt 写禁止贪功」？**  
真跑证明不够：模型仍输出「完整系统」。必须把份额变成**选材与程序化约束**。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 直接回应诚信风险 | 份额是 commit 代理，不等于代码价值 |
| 可解释（caution 带数字） | 阈值需调；边界仓（如 13.7%）要产品判断 |
| 与 fact sheet 同构 | 依赖 analyze 强制写入可解析 caution |

**5. 为什么不用其他同类技术？**  
- **只按文件 blame 行级归属：** 更准但重，MVP 未做。  
- **用户手改 Markdown：** 可作补充；chat 应能自动重生成。  
- **丢掉所有团队仓：** 过严，中高份额仓仍有模块级证据。

---

### ⑤ Fact sheet（写作路径上的事实清单）

**1. 是什么？**  
一组带 evidence 的可核验条目，注入 prompt 约束 LLM。Phase 1 从 `RepoStatsBundle` 构建；Phase 4 在无完整 stats 时用画像 caution **合成**写作用精简清单。

**2. 为什么要在这个项目用它？**  
画像与简历都容易幻觉数字/主导地位。把 `author_share` / `low_author_share` 写成条目，Writer/Critic 才有对账依据。

**3. 为什么不用「相信 SkillProfile 字段就够了」？**  
Profile 是 LLM 产物，自身可能漏 caution；且 Writer 原先常拿不到结构化份额。强制 caution + 合成 fact sheet 双保险。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 反幻觉有对账物 | 要维护构建与注入链路 |
| 可从 stats 或 profile 来源 | 合成清单不如完整 stats 丰富 |
| 与评测/回归友好 | 断链时形同虚设（见坑 2） |

**5. 为什么不用其他同类技术？**  
- **RAG 检索当唯一事实源：** 检索是相关片段，不是「份额」这种统计事实。  
- **数据库约束强制字段：** 有价值，但生成时仍需进 prompt 才约束模型。

---

### ⑥ 超时与降级（Agent 产品化）

**1. 是什么？**  
对慢/易失败步骤设超时；失败时回退到较弱但可用的路径（硬检查、向量分），并给模型/用户明确「不要死磕重试」的契约。

**2. 为什么要在这个项目用它？**  
Writer-Critic + 搜岗打分在 Air 模型上动辄数十秒；一次超时若冒泡成工具失败，主模型会连打 `generate_resume`，体验崩盘。

**3. 为什么不用「加大 timeout 等到成功」？**  
用户以为卡死；费用与会话占用失控；仍无法解决坏 JSON 无限重试。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 失败可继续、可解释 | 降级结果质量可能变差 |
| 抑制工具风暴 | 要设计清晰的成功/失败语义 |
| 与进度展示互补 | 超时阈值需按模型调 |

**5. 为什么不用其他同类技术？**  
- **队列 + 异步任务中心：** 产品化可做，CLI MVP 过重。  
- **无限自动换模型重试：** 成本不可控；应询问用户。

---

### ⑦ 粗排 / 精排与延迟预算（补充）

**1. 是什么？**  
粗排从大库拉候选（hybrid/RRF）；精排对 top 重打分（LLM/CE）。是否上精排由质量收益 vs 延迟决定。

**2. 为什么要在这个项目用它？**  
简历生成已有多轮 LLM；再对素材做 LLM rerank 常多等 1–2 分钟。MVP 选择 hybrid 顺序直接进 Writer。

**3. 为什么不用「生成路径也强制 LLM rerank」？**  
真跑显示边际收益不如把时间留给 Writer-Critic；Phase 5 可用开关/CE 再开。

**4. 有什么优缺点？**  

| 优点 | 缺点 |
|------|------|
| 端到端更可控 | 粗排噪声可能进 Writer |
| 与 Phase 3 评测能力仍兼容 | 需文档写清「刻意跳过」 |

**5. 为什么不用其他同类技术？**  
见 Phase 3 复盘：CE 对照、RRF 等；本阶段决策是**预算分配**，不是否定精排。

---

### 速记对照表

| 我用了 | 为什么不用常见替代 | 一句话 |
|--------|--------------------|--------|
| Writer-Critic 同会话 | 一次生成到底 / 拆两子 agent | 短轮次可修，隔离不划算 |
| 硬过滤 ∪ LLM Critic | 全交给模型审 | 可判定规则要确定性 |
| SubAgent = Tool | 只靠多角色提示 / 上框架 | 隔离上下文才叫多智能体 |
| author_share 进选材 | 只写「禁止贪功」 | 诚信靠规则不只靠礼貌 |
| fact_sheet 合成注入 | 接口留空 / 只信 profile | 清单必须进最终 prompt |
| 超时降级 + 【已完成】 | 加大超时死等 / 自动连打 | 先可继续，再谈完美 |
| 跳过简历 LLM rerank | 处处精排 | 延迟预算让给写作环 |

## 5. 下一步

1. （可选）补「子 agent 拆分前后」同任务 token / 轮次对比结论（MVP 验收文案）。  
2. （可选）独立 `export` 命令（多格式）。  
3. **进入 Phase 5**：`evals/` golden + runner + 提示词 ≥2 轮调优；把贪功 / 超时 / 坏 JSON / 连打工具做成回归。  
4. 真跑提醒：改代码后**重启 chat** → **再 repo_analyst**（刷新带数字的 caution）→ 再生成。

## 6. 30 秒速记卡

- **做了什么：** Writer-Critic 简历流水线 + 子 agent 分析/搜岗 + 贡献归属选材 + E2E 超时/进度/并发加固。  
- **Gate：** chat 能选岗写出 `resume_draft.md`；低份额仓默认不贪功；相关单测绿。  
- **一句话：** 能检索 ≠ 能诚实写作；份额进选材，硬过滤进 Critic。  
- **面试答法：** 见 §4 五问（Writer-Critic / 子 agent / Attribution 最常问）。  
- **流程：** 见 §1.5。  
- **踩坑：** fact_sheet 断链、Critic 超时、成功后重跑工具、子 agent 参数类型。  
- **下一站：** Phase 5 评测与提示词调优。
