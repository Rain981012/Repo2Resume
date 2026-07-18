# Phase A2 — 失败案例记录表

> 对应设计：[`02_phase_a2_iteration.md`](./02_phase_a2_iteration.md) §2。  
> 填写时**不要**写入姓名/电话/邮箱等隐私；可引用 `skill/runs/<日期>/` 路径。  
> 原始笔记：[`record`](./record)。

## 标签速查

| 标签 | 含义 |
|------|------|
| `hallucination` | 无出处数字、硬贴无证据技能、低 share 冒充主导 |
| `wrong_highlight` | 项目/bullet 与 JD 不相关、排序反了；或亮点过空不可用 |
| `tone_style` | 空话、过长、语种错；或对用户展示信息过简/不当 |
| `scoring` | 打分离谱、过期岗进推荐、偏好与技能字段混淆 |
| `profile` | 主方向判错、语言占比口算、画像要点过空 |
| `process` | 跳过 HITL、未搜满门槛（现 ≥50）、未跑 Critic、搜岗交互摩擦 |
| `other` | 其它（在「说明」列写一句） |

---

## 总表（每条失败一行，便于统计）

| Case ID | 轮次 | 日期 | 标签 | 症状（一句话） | 期望 | 根因层 | 修改文件 | 已复测？ |
|---------|------|------|------|----------------|------|--------|----------|----------|
| Case-20260714-1 | 1 | 2026-07-14 | `profile`, `wrong_highlight` | `highlights_pool` 只有 commit 标题+行数，未写清重构了什么 | claim 含可核对的具体改动点（模块/交互/文件证据） | 01 | `prompts/01_skill_profile.md` | 待复测 |
| Case-20260714-2 | 1 | 2026-07-14 | `wrong_highlight`, `tone_style` | Step4 项目经历偏框架空话（上游亮点过空导致） | 有具体改动点的可投递 bullet；上游不足时标 gaps | 01→03 | `01`（主）；`03` 视复测 | 待复测 |
| Case-20260714-3 | 1 | 2026-07-14 | `scoring` | 已截止/下线岗位进入推荐 top | 过期岗 `recommend:false` 且 `score=0`，不得进默认 Top | 02 | `prompts/02_job_search.md` | 待复测 |
| Case-20260714-4 | 1 | 2026-07-14 | `process`, `other` | Top 列表未展示可点击网址 | 每条推荐岗必须带有效 `url` 并展示给用户 | 02 | `prompts/02_job_search.md` | 待复测 |
| Case-20260714-5 | 1 | 2026-07-14 | `tone_style`, `scoring` | 对用户展示原始分数，信息噪音大 | 分数只写入归档 JSON；对用户用文字分析 | 02 | `prompts/02_job_search.md`；`SKILL.md` | 待复测 |
| Case-20260714-6 | 1 | 2026-07-14 | `other` | 只有单一综合 Top5，难同时看「匹配」与「待遇」 | 两套 Top5：技能匹配 / 待遇优先 | 02 | `prompts/02_job_search.md` | 待复测 |
| Case-20260714-7 | 1 | 2026-07-14 | `tone_style` | 职位要点/verdict 过短，缺匹配与缺口说明 | 每岗固定：匹配点/偏好/薪资/缺口 | 02 | `prompts/02_job_search.md` | 待复测 |
| Case-20260714-8 | 1 | 2026-07-14 | `process` | 搜岗时频繁 WebFetch，用户需反复同意授权 | 优先搜索摘要建池；完整 JD 少次批量拉取 | 02 / SKILL | `prompts/02_job_search.md`；`SKILL.md` | 待复测 |
| Case-20260716-1 | 2 | 2026-07-16 | `profile`, `tone_style` | `tech_stack` 扁平罗列难读；`gaps_or_cautions` 命名不清 | 分类汇总 tech_stack；字段改名 `caution` | 01 / SKILL | `prompts/01_skill_profile.md`；`SKILL.md` | 待复测 |
| Case-20260716-2 | 2 | 2026-07-16 | `process` | 打分池偏小(≥20)、缺大厂专项、展示后不询问是否续搜；Top 条数不可调 | 池 ≥50；大厂专项；`top_n` 默认5最大10；展示后必问是否继续搜 | 02 / SKILL | `prompts/02_job_search.md`；`SKILL.md` | 待复测 |
| Case-20260716-3 | 2 | 2026-07-16 | `tone_style`, `wrong_highlight` | 简历正文出现占比/commit/免责声明；bullet 过短；总结写个人角色 | 严格 STAR、每条2–4句；总结仅产品定位；正文禁占比/commit/免责声明 | 03 / 04 | `prompts/03_resume_writing.md`；`04_critic_review.md` | 待复测 |

**根因层**填：`01` / `02` / `03` / `04` / `SKILL` / `stats数据` / `用户偏好未问清`

**本轮 runs：** `skill/runs/2026-07-14/`（`stats.json` / `skill_profile.json` / `jobs.json` / 项目经历草稿）

---

## 明细

### Case-20260714-1

- **round:** 1
- **tags:** `[profile, wrong_highlight]`
- **runs_path:** `skill/runs/2026-07-14/skill_profile.json`
- **symptom:** `highlights_pool` 类似「重构冒险游戏前端（commit: …），改动集中在 frontend（约 +1223 行）」，没有具体重构内容。
- **expected:** 打开 diff/触及文件后写出 2–4 个可核对改动点；写不出则进 `gaps_or_cautions`，禁止用 subject+行数凑数。
- **root_cause:** 01 未强制「超越 commit subject」的源码核对；stats  alone 不够支撑具体 claim。
- **fix:** `01_skill_profile.md`：硬性规则 8（禁止空壳亮点）+ 示例改为含 path/`git show` + HITL 展示门禁（2026-07-16）。
- **verified:** 待复测（建议同仓 NLP_GAME 重跑 Step2）

### Case-20260714-2

- **round:** 1
- **tags:** `[wrong_highlight, tone_style]`
- **runs_path:** `skill/runs/2026-07-14/resume_jd_*_项目经历.md`
- **symptom:** 项目经历多为「完成前端重构相关改动」类框架句，三份 JD 差异主要在措辞而非事实粒度。
- **expected:** bullet 能落到组件/交互/目录级具体点；无证据不硬写。
- **root_cause:** 上游 01 亮点过空传导到 03；03 本身未强制回源码补细节。
- **fix:** 先修 01（已落地）；若复测 Step4 仍空再改 03。
- **verified:** 待复测（依赖 Case-1 通过后再看 Step4）

### Case-20260714-3

- **round:** 1
- **tags:** `[scoring]`
- **runs_path:** `skill/runs/2026-07-14/jobs.json`
- **symptom:** Top1（如软牛 Vue 岗）渠道已截止仍获高推荐。
- **expected:** 已结束/下线/暂停 → `recommend:false` 且 `score=0`，不得进默认推荐 Top；可放「已过期备选」。
- **root_cause:** 02 未把时效写入 `recommend` 门禁。
- **fix:** `02_job_search.md`：`listing_status` → `recommend:false` + 强制 `score=0`（2026-07-16）。
- **verified:** 待复测

### Case-20260714-4

- **round:** 1
- **tags:** `[process, other]`
- **runs_path:** `skill/runs/2026-07-14/`（对话展示）
- **symptom:** 推荐列表未带可点击网址。
- **expected:** 每条展示岗必须含有效 URL；无链接不进展示池。
- **root_cause:** 02 交互节未要求「展示时打印 url」。
- **fix:** `02_job_search.md` 展示契约强制 url（2026-07-16）。
- **verified:** 待复测

### Case-20260714-5

- **round:** 1
- **tags:** `[tone_style, scoring]`
- **runs_path:** `skill/runs/2026-07-14/jobs.json`
- **symptom:** 对用户展示 score / skill_score / preference_score，噪音大。
- **expected:** 分数仅写入归档 JSON；对用户用结构化文字分析。
- **root_cause:** 02/SKILL 默认「展示评分最高」。
- **fix:** `02_job_search.md` + `SKILL.md` Step 3：默认不展示原始分（2026-07-16）。
- **verified:** 待复测

### Case-20260714-6

- **round:** 1
- **tags:** `[other]`
- **runs_path:** `skill/runs/2026-07-14/jobs.json`
- **symptom:** 只有单一综合 Top5，无法同时看「最匹配」与「待遇最好」。
- **expected:** Top5-A 技能+偏好匹配；Top5-B 待遇/大厂优先（仍须在招且写清技能缺口）。
- **root_cause:** 02 只定义一套 top5。
- **fix:** `02_job_search.md` 双 Top5（2026-07-16）。
- **verified:** 待复测

### Case-20260714-7

- **round:** 1
- **tags:** `[tone_style]`
- **runs_path:** `skill/runs/2026-07-14/jobs.json`（原 `verdict`）
- **symptom:** 要点分析过短，缺「为何匹配 / 偏好 / 薪资 / 缺什么技能」。
- **expected:** 每岗固定四段分析（匹配点、偏好、薪资、缺口）。
- **root_cause:** 02 对展示深度无模板约束。
- **fix:** `02_job_search.md` 增加 `analysis` 四段模板（2026-07-16）。
- **verified:** 待复测

### Case-20260714-8

- **round:** 1
- **tags:** `[process]`
- **runs_path:** （对话过程）
- **symptom:** 搜岗阶段频繁 fetch 网页，用户需反复同意命令/授权。
- **expected:** 优先用搜索结果摘要建池；仅对进入打分池的岗拉取完整 JD，且尽量并行、少轮次。
- **root_cause:** 02/SKILL 未约束搜岗工具策略。
- **fix:** `02_job_search.md` + `SKILL.md` 搜岗执行策略（2026-07-16）。
- **verified:** 待复测

### Case-20260716-1

- **round:** 2
- **tags:** `[profile, tone_style]`
- **runs_path:** `skill/runs/2026-07-16/skill_profile.json`；笔记见 `record` 第二轮 Step1
- **symptom:** 技术栈把所有名词平铺；`gaps_or_cautions` 对用户语义不清。
- **expected:** `tech_stack` 按语言/框架/数据库/工具分类；字段改名为 `caution` 并说明含义。
- **root_cause:** 01 允许扁平数组且命名偏内部术语。
- **fix:** `01_skill_profile.md` 硬性规则 9 + 字段 `caution`；`SKILL.md` Step2 展示契约（2026-07-16）。
- **verified:** 待复测

### Case-20260716-2

- **round:** 2
- **tags:** `[process]`
- **runs_path:** `skill/runs/2026-07-16/jobs_ranked.json`；笔记见 `record` 第二轮 Step3
- **symptom:** 打分池偏小；缺大厂专项；Top 条数固定；展示后未问是否续搜；个别岗随后失效。
- **expected:** 池 ≥50；大厂专项搜；开搜前问 `top_n`(1–10，默认5)；展示后必问是否继续搜；失效岗移出推荐。
- **root_cause:** 02/SKILL 门槛与交互契约不足。
- **fix:** `02_job_search.md` + `SKILL.md` Step3（2026-07-16）。
- **verified:** 待复测

### Case-20260716-3

- **round:** 2
- **tags:** `[tone_style, wrong_highlight]`
- **runs_path:** `skill/runs/2026-07-16/resume_draft_v*.md`；笔记见 `record` 第二轮 Step4
- **symptom:** 正文写占比/commit/「多人协作」；bullet 过短；项目总结夹带个人角色。
- **expected:** 严格 STAR、每条 2–4 句；总结仅产品定位；正文禁占比/commit/免责声明；可对照 `templates/简历-伍思远.pdf` 密度。
- **root_cause:** 03 未强制 STAR/长度与正文禁区；04 未检查这些项。
- **fix:** `03_resume_writing.md` + `04_critic_review.md`（2026-07-16）。
- **verified:** 待复测

### Case-20260717-1

- **round:** 3
- **tags:** `[process]`
- **runs_path:** `skill/runs/2026-07-17/jobs_ranked.json`；笔记见 `record` 第三轮 Step2
- **symptom:** 大厂池不够全；无论用户是否要求，都要搜完整大厂清单耗时过长/不受控。
- **expected:** 补充完整分层大厂清单（超一线/一线/垂直赛道/中型企业/科技外企）；默认只搜超一线+一线主流大厂，用户显式要求「搜所有大厂」才扩大范围，且扩大前必须提示耗时增加并确认。
- **root_cause:** 02/SKILL 大厂池过窄且无分层与范围控制机制。
- **fix:** `02_job_search.md`「大厂池分层清单」+ 范围与耗时规则；`SKILL.md` Step3（2026-07-17）。
- **verified:** 待复测

### Case-20260717-2

- **round:** 3
- **tags:** `[process, wrong_highlight]`
- **runs_path:** 笔记见 `record` 第三轮 Step2（nowcoder 岗位 418529/434608/438733）
- **symptom:** 页面已标注薪资但正文未写 → 误判「薪资未写明」；页面已标「已结束」但 JD 文本截止日期仍在未来 → 误判仍可推荐；Top 待遇优先里链接多为大厂招聘首页/列表页而非具体 JD 详情页。
- **expected:** 薪资采集不限于 JD 正文，页面任意位置展示即须采入 `salary_text`；`listing_status` 判定以页面自身状态为唯一依据，优先级高于 JD 文本日期；Top-N 展示 URL 必须是具体职位详情页，仅有列表页不进默认 Top-N。
- **root_cause:** 02 未区分「JD 正文」与「页面展示」两类信息源；时效字段判定优先级未写明；Top-N 展示契约未限定 URL 粒度。
- **fix:** `02_job_search.md` 第二步第5–6条 + 展示契约第2条；`SKILL.md` Step3（2026-07-17）。
- **verified:** 待复测

### Case-20260717-3

- **round:** 3
- **tags:** `[tone_style]`
- **runs_path:** 笔记见 `record` 第三轮 Step4；参考 `templates/简历-伍思远.pdf`
- **symptom:** 03 的「好的输出」示例写成叙事段落（「业务需要…且必须…围绕该目标…」），S/T 铺陈过长、句子间用因果转折词衔接，不符合真实简历里「短标签+动作+技术栈；分号衔接结果」的高密度写法。
- **expected:** bullet 以 `**短标签**：` 开头；STAR 压缩进短语与分号，不写成完整叙事句；长度改用「约 80–180 汉字」描述而非「2–4 句话」。
- **root_cause:** 03 的示例与规则本身引导出叙事体，04 也未检查标签与分号密度。
- **fix:** `03_resume_writing.md` 输出格式/风格/Few-shot（新好例+原好例降级为坏例B）；`04_critic_review.md` 审查清单（2026-07-17）。
- **verified:** 待复测

---

<!-- 复制一块空白模板：

### Case-

- **round:**
- **tags:** `[]`
- **runs_path:**
- **symptom:**
- **expected:**
- **root_cause:**
- **fix:**
- **verified:** 否

-->
