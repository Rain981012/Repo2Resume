# put_in_practice：开发过程学习复盘

本目录记录 Repo2Resume **每一步做了什么、学到什么、面试怎么讲**，不是产品 README。

## 怎么用（自动化）

已配置项目 Skill + 斜杠命令：

| 方式 | 怎么触发 |
|------|----------|
| 自然语言 | 「更新 put_in_practice：我刚完成了 Phase A1 的 prompts」 |
| 斜杠命令 | Cursor 里运行 **Update put-in-practice**（`.cursor/commands/update-put-in-practice.md`） |
| Skill | `.cursor/skills/put-in-practice/SKILL.md`（提到复盘/实践笔记时会被选用） |

Agent 会：

1. 总结**本步完成了什么**  
2. 写清**做法 · 影响 · 亮点 · 踩坑**  
3. 补上**面试向知识点**  
4. 对**同一文档查漏补缺**，不整篇重写  

## 目录约定

```text
docs/put_in_practice/
├── README.md                 ← 本说明
├── PhaseA/
│   └── 01_phase_a1_skill_prototype.md
├── PhaseB/
│   ├── 01_phase_1_repo_analysis.md   ← Phase 0/1：CLI 分析 + fact sheet 验收
│   └── 02_phase_2_harness.md          ← Phase 2：手写 agent loop + chat + 会话持久化
└── …
```

## 推荐触发语示例

```text
更新 put_in_practice：我完成了 Phase A1 四组 prompts 并同步到 skill/prompts/
复盘一下刚改的 job_search_locale 默认中文，补进 A1 文档
开始 A2 第一轮真跑后，新建或更新 PhaseA 的 A2 复盘文档
复盘 Phase 1 验收，写到 docs/put_in_practice/PhaseB
```
