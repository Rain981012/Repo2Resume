# Phase A2 — 真实数据迭代（设计与执行流程）

> 对应 `MVP_PLAN.md` §2 Phase A2（约 0.5–1 天）。  
> 前置：Phase A1 已完成（`skill/SKILL.md` + `skill/prompts/01–04` + `skill/scripts/git_stats.py` 已启用）。  
> 目的：用真实仓库验证 Skill 闭环，迭代提示词，并把输入输出归档为阶段 B 评测种子。

---

## 0. 任务总览（编码方式）


| #    | 任务                    | 方式          | 说明                                                        |
| ---- | --------------------- | ----------- | --------------------------------------------------------- |
| A2-1 | 用真实仓库跑全流程 ≥ **3** 轮   | **【手写】**    | 人工驱动 Cursor Skill；你确认每个 ⏸                                 |
| A2-2 | 每轮记录失败案例并改提示词         | **【手写】**    | 分类：幻觉 / 亮点选错 / 语气等；只改 `skill/prompts/` 或 `skill/SKILL.md` |
| A2-3 | 归档输入输出到 `skill/runs/` | **【AI 生成】** | 按下方清单落盘；可让 Agent 代写目录与 `meta.json`，你核对                    |
| A2-4 | 写一页结论                 | **【手写】**    | 哪一环最弱、提示词关键教训 → 建议写入 put_in_practice                      |


**Gate（进入阶段 B 的前提）**：产出的**项目经历**经你人工审阅大约有 **70 分**水平。若宿主 Agent 全力跑仍不及格，先修流程/提示词，再写 CLI。

---

## 1. 【手写】全流程 ≥ 3 轮

### 1.1 每轮怎么启动

在 Cursor 中触发 Repo2Resume Skill，例如：

```text
用 Repo2Resume Skill，根据 local_repos（或我指定的真实仓）分析我的贡献，
走完全流程：统计 → 画像 → 搜岗 → 选定 JD → 项目经历 → 审稿 → 归档到 skill/runs/。
我的 git author 是：…
```

仓库建议：

- 优先 `local_repos/` 内已有仓（含多人协作仓，练 `--author`）
- 至少 1 轮用「你真正想投」的真实本地仓
- 3 轮尽量有差异：换仓 / 换 JD / 换 `job_search_locale`（如一轮 `zh-CN`，一轮试 `en`）

### 1.2 每轮必须走完的路径（对齐 `skill/SKILL.md`）

```text
Step 1  git_stats → stats.json
Step 2  01 画像 ⏸ 确认主方向 / 技术栈 / 概括职位类型（gaps 可选）
Step 3  02 搜岗 ≥20、top5、偏好与 locale ⏸ 选定目标 JD
Step 4  03 项目经历 → 04 Critic ≤2 轮
Step 5  ⏸ 定稿 → 写入 runs/（投递版可去 src 注释）
```

**人工流程含义：** 三个 ⏸ 必须你本人拍板；不要让 Agent 跳过确认「自动进入下一步」。

### 1.3 轮次记录表（建议每轮填一行）


| 轮次  | 日期   | 仓库           | author  | 目标 JD                                                                                                     | locale(搜/写)   | 主观分(1–10) | 主要问题分类 | 是否已改 prompt |
| --- | ---- | ------------ | ------- | --------------------------------------------------------------------------------------------------------- | ------------- | --------- | ------ | ----------- |
| 1   | 7.14 | **NLP_GAME** | Rain Wu | Vue3 + FastAPI 游戏；你主要是 FE refactor，占比约 **11%** → 宜作辅助 Vue 前端工程师（初级）- 前端开发（JavaScript / Vue3）- 全栈实习生（偏前端） | zh-CN / zh-CN |           |        |             |
| 2   |      |              |         |                                                                                                           |               |           |        |             |
| 3   |      |              |         |                                                                                                           |               |           |        |             |


可放在：`skill/runs/_a2_round_log.md` 或 put_in_practice 的 A2 文档中。

---

## 2. 【手写】失败案例 → 改提示词

### 2.1 分类标签（记录时打标，便于统计最弱环节）


| 标签                | 含义                          | 优先改哪里                        |
| ----------------- | --------------------------- | ---------------------------- |
| `hallucination`   | 无出处数字、硬贴无证据技能、低 share 冒充主导  | 01 硬规则 / 03 事实约束 / 04 事实一票否决 |
| `wrong_highlight` | 项目或 bullet 与 JD 不相关、排序反了    | 03 挑亮点；02 选定 JD 是否合适         |
| `tone_style`      | 空话、过长、不像简历、语种错              | 03 风格；`resume_locale`        |
| `scoring`         | 打分离谱、偏好/技能混进 matched_skills | 02 打分公式与 breakdown           |
| `profile`         | 主方向判错、语言占比口算                | 01 推断规则与字段                   |
| `process`         | 跳过 HITL、未搜满 ≥20、未跑 Critic   | `SKILL.md` 门禁表述              |
| `other`           | 其它（需一句话说明）                  | 视情况                          |


### 2.2 每条失败案例最小字段

```markdown
### Case-YYYYMMDD-N
- round: 1
- tags: [hallucination, …]
- symptom: （看到了什么）
- expected: （应该怎样）
- root_cause: （提示词/流程/数据哪一层）
- fix: （改了哪个文件的哪条规则）
- verified: 否 / 已在下一轮复测
```

建议集中写在：`[a2_failure_cases.md](./a2_failure_cases.md)`（可提交到 git；勿含隐私联系方式）。

### 2.3 改提示词纪律

1. **只改正式路径**：`skill/prompts/*.md`、`skill/SKILL.md`（已无 `handwrite/`）。
2. **一次改一类问题**，改完用下一轮验证，避免多处同时大改无法归因。
3. 改完后可用：「更新 put_in_practice」把教训补进复盘文档。

---

## 3. 【AI 生成】归档到 `skill/runs/`

### 3.1 目录约定

与 `skill/SKILL.md`「归档」一致：

```text
skill/runs/<YYYY-MM-DD>/          # 同日多轮用 <YYYY-MM-DD>-<HHMM>/
├── stats.json
├── skill_profile.json
├── job_directions.md             # 若有
├── jobs_ranked.json
├── target_jd.md
├── resume_draft_v*.md            # 项目经历各版草稿
├── resume_final.md               # 定稿（现阶段=项目经历）
├── user_feedback.md
└── meta.json
```

`skill/runs/` 已在 `.gitignore`：**含个人数据，不要 push 到远程。**  
评测种子可日后筛脱敏样本再进仓库（阶段 B）。

### 3.2 `meta.json` 最小字段

```json
{
  "model": "本次使用的模型名",
  "date": "ISO 日期或本地日期时间",
  "round": 1,
  "target_job": "选定职位标题 + 公司（若有）",
  "resume_locale": "zh-CN",
  "job_search_locale": "zh-CN",
  "repos": ["…"],
  "authors": ["…"],
  "subjective_score": 7,
  "failure_tags": ["hallucination"]
}
```

### 3.3 Agent 可代劳、你必须核对的

- 目录是否按轮次分开、文件是否齐全  
- `resume_final.md` 是否真是你确认过的版本  
- `<!-- src -->` 存档版是否保留（投递版另存时可删除）

---

## 4. 【手写】一页结论

建议路径：`docs/put_in_practice/PhaseA/02_phase_a2_iteration.md`（跑完后再写；也可用 put-in-practice Skill 生成骨架后你填）。

**必须回答：**

1. **哪一环质量最弱？**（Step 1–5 / 01–04 中选一，给证据）
2. **提示词的关键教训**（3–5 条，可执行，不要空话）
3. **是否达到 ~70 分 Gate？** 过 / 不过；不过则下一步还改什么
4. **带给阶段 B 的种子**：`runs/` 里哪几轮最值得做 golden set

---

## 5. 验收清单（Definition of Done）

- 完整人工流程 ≥ 3 轮（每轮含 HITL）  
- `[a2_failure_cases.md](./a2_failure_cases.md)` 中有分类记录，且至少有一次「改 prompt → 复测」  
- `skill/runs/` 下至少 3 个轮次目录，核心文件齐全  
- 一页结论已写  
- 你主观认为项目经历 ≥ ~70 分 → 可开阶段 B；否则继续 A2 迭代

---

## 6. 与其它文档的关系


| 文档                             | 关系                         |
| ------------------------------ | -------------------------- |
| `skill/SKILL.md`               | 执行时的流程真源                   |
| `skill/prompts/01–04`          | 本阶段主要修改对象                  |
| `docs/put_in_practice/PhaseA/` | 面试向复盘；A2 结论落这里             |
| `docs/design_docs/MVP_PLAN.md` | 计划与 Gate 定义                |
| 阶段 B                           | 复用提示词 + `runs/` 做 evals 种子 |


---

## 7. 建议执行顺序（一天内）

```text
上午  第 1 轮全流程 + 归档 + 记 1–2 个失败案例
中午  改一类 prompt → 第 2 轮验证
下午  第 3 轮（换仓或换 JD）→ 整理 failure 表 → 写一页结论 → 判 Gate
```

