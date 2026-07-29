# put_in_practice：开发过程学习复盘

本目录记录 Repo2Resume **每一步做了什么、学到什么、面试怎么讲**，不是产品 README。

## 结构规范（强制）

**所有新建 / 更新的复盘必须遵守：**

→ [`RETROSPECTIVE_SPEC.md`](./RETROSPECTIVE_SPEC.md)

范本：`PhaseB/03_phase_2_5_lifecycle_hooks.md`、`PhaseB/04_phase_3_retrieval_jobs.md`。

核心结构：§1 定位 → §1.5 流程 → §2 完成项 → §3 做法/影响/亮点/踩坑 → §4 五问知识点 → §5 下一步 → §6 速记卡。

## 怎么用（自动化）

已配置项目 Skill + 斜杠命令：

| 方式 | 怎么触发 |
|------|----------|
| 自然语言 | 「更新 put_in_practice：我刚完成了 Phase A1 的 prompts」 |
| 斜杠命令 | Cursor 里运行 **Update put-in-practice**（`.cursor/commands/update-put-in-practice.md`） |
| Skill | `.cursor/skills/put-in-practice/SKILL.md`（提到复盘/实践笔记时会被选用） |

Agent 会：

1. 先读 **`RETROSPECTIVE_SPEC.md`**
2. 总结**本步完成了什么**（§2）
3. 写清**做法 · 影响 · 亮点 · 踩坑**（§3）
4. 按**五问模板**补面试知识点（§4）
5. 对**同一文档查漏补缺**，不整篇重写

## 目录约定

```text
docs/put_in_practice/
├── README.md                 ← 本说明
├── RETROSPECTIVE_SPEC.md     ← ★ 复盘结构规范（强制）
├── PhaseA/
│   └── 01_phase_a1_skill_prototype.md
├── PhaseB/
│   ├── 01_phase_1_repo_analysis.md   ← Phase 0/1：CLI 分析 + fact sheet 验收
│   ├── 02_phase_2_harness.md          ← Phase 2：手写 agent loop + chat + 会话持久化
│   ├── 03_phase_2_5_lifecycle_hooks.md ← Phase 2.5：lifecycle hooks（权限/观测/错误恢复）
│   ├── 04_phase_3_retrieval_jobs.md    ← Phase 3：RAG 检索 + 职位匹配
│   └── 05_phase_4_resume_subagent.md   ← Phase 4：Writer-Critic + 子 agent + 贡献归属
└── …
```

## 推荐触发语示例

```text
更新 put_in_practice：我完成了 Phase A1 四组 prompts 并同步到 skill/prompts/
复盘一下刚改的 job_search_locale 默认中文，补进 A1 文档
按 RETROSPECTIVE_SPEC 新建 Phase 5 复盘
把 05 的 §4 补成五问体
```
