# Phase B / Phase 1 实践复盘：仓库分析闭环（面试复习稿）

> 项目：Repo2Resume  
> 阶段：阶段 B · **Phase 0 骨架已就绪** + **Phase 1 仓库分析（含 fact sheet 手写与验收）**  
> 用途：求职面试复习「CLI 如何把 Skill 期验证过的防幻觉规则工程化」  
> 对应文档：`docs/design_docs/MVP_PLAN.md` §3 Phase 0–1、`DESIGN.md`（事实清单 / EvidenceRef）  
> 验收日：2026-07-19（`local_repos/` 四仓真跑）

---

## 1. 一句话定位

阶段 A 在 Cursor Skill 里验证了「git 统计 → 可溯源画像」；阶段 B Phase 1 把同一条链路固化成独立 CLI：`mine → tech_detector → fact_sheet → profiler`，用程序生成的事实清单约束 LLM，而不是只靠提示词口头说「不要编造」。

面试可说：

> 我把阶段 A 的 fact sheet 规则落成代码：先确定性抽取数字，再让 LLM 只能重组这些事实。验收时语言占比与 caution 与 stats 一致，重跑走缓存。

---

## 2. 本阶段完成了什么（对照计划）

### 2.1 Phase 0 — 项目骨架（前置，本步默认已具备）

| MVP 任务 | 状态 | 说明 |
|---|---|---|
| `pyproject.toml` / src 布局 / 入口 | ✅ | `python -m repo2resume.cli` |
| Typer CLI 骨架 | ✅ | `init / analyze / …` |
| `config.py` + LiteLLM | ✅ | `~/.repo2resume/config.toml` |
| Redis + `CacheBackend` 降级 | ✅ | 验收时 Redis 可用（`cache backend: redis`） |
| `storage/db.py` / pytest / CI | ✅ | 本机 `30 passed` |

### 2.2 Phase 1 — 仓库分析

| MVP 任务 | 状态 | 交付物 |
|---|---|---|
| `analysis/git_miner.py`【AI 辅助】 | ✅ | 多仓、`--author`、聚合 `RepoStatsBundle` |
| `analysis/tech_detector.py`【AI 生成】 | ✅ | 规则检测依赖/语言 → `TechStack` |
| `storage/models.py`【AI 辅助】 | ✅ | `EvidenceRef` / `FactEntry` / `SkillProfile` 等 |
| `analysis/profiler.py`【AI 辅助】 | ✅ | Jinja 提示词 + JSON 校验重试；注入 `fact_sheet_block` |
| **事实清单 fact sheet【手写】** | ✅ | `analysis/fact_sheet.py` + `tests/test_fact_sheet.py` |
| 分析缓存【AI 生成】 | ✅ | per-repo `analysis:{hash}:{head}:…`；画像另有 `profile_cache_key` |
| 单测 fixture | ✅ | miner / tech / fact_sheet / cli 等；全绿 **30 passed** |

### 2.3 本步交付物（路径）

| 路径 | 说明 |
|---|---|
| `src/repo2resume/analysis/fact_sheet.py` | `build_fact_sheet`：语言份额 / 贡献量 / 依赖 / low_author_share |
| `src/repo2resume/analysis/pipeline.py` | `run_analyze`：mine → tech → facts → profile |
| `src/repo2resume/prompts/analyzer/build_skill_profile.j2` | 画像 system prompt，含 fact_sheet 块 |
| `tests/test_fact_sheet.py` | TDD 契约测试（手写模块的验收钉） |

**搭建 vs 验收：** 代码此前已基本齐；**本步完成的是 Phase 1 Gate 真跑验收 + 复盘落盘**。

---

## 3. Phase 1 验收记录（Gate）

**验收标准（MVP）：** `analyze` 输出画像，Rich 展示语言占比；重跑命中缓存。

### 3.1 命令与过滤作者

`config.toml` 里的 `email`（gmail）与 `local_repos` 历史作者不一致 → 用仓库内真实邮箱过滤（否则 `total_author_commits=0`）：

```bash
.venv/bin/python -m repo2resume.cli analyze \
  --author siyuan9@ualberta.ca \
  --author 25045524g@connect.polyu.hk \
  --author 43023434+Rain981012@users.noreply.github.com
```

四仓：`ADA-group-project` / `NLP_GAME` / `groupproject-team_1` / `socialdistribution`。

### 3.2 结果摘要

| 检查项 | 结果 |
|---|---|
| pytest | **30 passed**（需非沙箱；沙箱下 `git init` hooks 会误红） |
| stats | `repo_count=4`，`total_author_commits=69` |
| 语言份额 | Python 0.337 / Jupyter 0.317 / TypeScript 0.184 / …（与表格一致） |
| fact sheet | 20 条；含 7 条 `summary.overall_language_share.*`；`NLP_GAME` / `socialdistribution` 有 `low_author_share` |
| 画像 | `primary_direction=Python 后端开发`；`coding_language[].evidence.source=summary.overall_language_share` |
| caution | 与 fact sheet 阈值一致（0.111 / 0.137 均 &lt; 0.15） |
| 缓存 | 四仓均 `cache hit analysis:…`；重跑约数秒返回（mine + profile 均命中） |
| Rich UI | 语言表 + Tech stack 分类表 + Caution 列表 |

**Gate 判断：通过。** 数字来自 stats/fact sheet，画像未口算语言占比；低贡献仓进入 caution。

---

## 4. 做法 · 影响 · 亮点 · 踩坑

### 4.1 做了什么

- **手写** `build_fact_sheet`：按脚手架填空完成四类事实（语言 / commits+share / dependencies / low_author_share）。  
- **流水线接线**：`safe_fact_sheet` → `Profiler.build_profile(..., fact_sheet=facts)` → 模板渲染 `fact_sheet_block`。  
- **验收**：`--stats-only` 核对抽取 → 全量 analyze 核对画像 → 重跑核对缓存。

### 4.2 对项目的影响

- 阶段 A「只信 stats」从 Skill 约定变成 **CLI 硬数据路径**：LLM 输入里多了一块程序生成的权威列表。  
- Phase 2 把 `analyze` 包成 tool 时，fact sheet 可继续作 post-tool 核对钩子的输入。  
- 画像里的 `EvidenceRef` 与 fact key 命名空间对齐（`summary.*` / `{repo}.*`），便于后续程序化断言。

### 4.3 亮点（可上面试）

1. **权威源在代码侧，不在模型侧**：fact sheet 是确定性 transform；模型只做结构化重组。  
2. **条件事实**：`dependencies` 有则写、无则跳；`author_share < 0.15` 才 caution——把产品规则写成 `if`，可测。  
3. **脚手架教练学习法**：【手写】模块用填空 + 失败测试推进，而不是整文件生成——能讲清自己写过防幻觉核心。

### 4.4 踩坑

**坑 1：配置邮箱 ≠ git author → 统计全零**  
- 现象：`--author` 用 config 的 gmail 时 `total_author_commits=0`，fact sheet 仍有 repo 级 0 提交 + 全员 low_share。  
- 教训：分析入口应优先「从仓库列出作者再选」；config email 只是默认偏好，不能默认等于过滤键。  
- 面试升华：数据管道的第一失败模式是 **错误的 join key**，不是模型幻觉。

**坑 2：沙箱 / CI 环境跑带 `git init` 的单测**  
- 现象：临时目录写 hooks 报 `Operation not permitted`，表现为 8 failed。  
- 教训：本地验收用完整权限；CI 需允许 fixture 建 git 仓。

**坑 3：fact sheet 脚手架残留**  
- 现象：实现通过后文件里仍留 `"""填空"""` 死字符串与教学注释。  
- 教训：TDD 绿了之后做一次「删脚手架」提交，避免正式代码像练习卷。

**坑 4（边界）：caution 的 `evidence.source`**  
- 实现里曾指向 `*.low_author_share`（自引用）；语义上更宜指向触发字段 `*.author_share`。测试只查 key，故验收仍绿——说明 **契约测试要覆盖 evidence 语义**，否则边界会漏。

---

## 5. 本阶段知识点（面试向）

| 概念 | 本项目怎么练到 | 可背一句 |
|---|---|---|
| Fact sheet / 硬约束 | `build_fact_sheet` + prompt 注入 | 缩小模型自由裁量：先锁权威字段，再生成 |
| Evidence / 可溯源 | `EvidenceRef(source, detail?)` | 每条论断带指针，才能程序校验 |
| 规则引擎 vs LLM | `tech_detector` 规则；`profiler` LLM | 能规则化的别交给概率模型 |
| 结构化输出 + 重试 | Profiler 校验失败 ≤2 次重问 | Schema 是契约，重试是 harness |
| 分析缓存键 | repo 路径哈希 + head + 过滤条件 | 输入不变则跳过贵操作 |
| Author 过滤 | 多邮箱 OR；与 display name 解耦 | 贡献归属是数据问题，先于生成 |
| 【手写】分层 | fact sheet 教练模式 | 核心学习点禁止代写整文件 |

**短答示例**

- **Q：Fact sheet 和 RAG 有何不同？**  
  Fact sheet 锁定已抽取字段作硬约束；RAG 按需检索片段。可组合：检索候选后仍只允许引用 fact sheet 中的数字。

- **Q：为什么 language share 必须走 summary 而不是模型估算？**  
  占比是确定性聚合；让模型口算会引入不可审计误差，且与防幻觉目标冲突。

---

## 6. 下一步

1. （可选）清理 `fact_sheet.py` 脚手架注释；修正 low_share 的 `evidence.source`。  
2. （可选）`init`/config 与 `collect_authors` 对齐，避免默认邮箱踩坑。  
3. ~~**进入 Phase 2 Harness 核心**~~ ✅ 见 `docs/put_in_practice/PhaseB/02_phase_2_harness.md`  
   （`agent/tools.py` / `context.py` / `prompt_assembler.py` / ★ `loop.py` + chat 接线 + 会话持久化）  
4. **下一站：Phase 2.5 加固**（hooks / 权限 / observability / error recovery）→ Phase 3 检索。

---

## 7. 30 秒速记卡

- **做了什么：** CLI 分析闭环 + 手写 fact sheet；四仓真跑出画像。  
- **Gate：** 语言占比/caution 与 stats 一致；缓存命中；30 tests 绿。  
- **一句话：** 防幻觉 = 程序先写事实清单，模型只能重组。  
- **下一站：** 手写 agent loop，把 analyze 变成 tool。
