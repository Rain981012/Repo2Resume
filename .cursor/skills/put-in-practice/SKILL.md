---
name: put-in-practice
description: >
  维护 Repo2Resume 的 put_in_practice 学习复盘文档：总结本步完成项、做法与影响、
  亮点与踩坑、面试知识点；对已有文档查漏补缺而非整篇重写。
  结构必须遵守 docs/put_in_practice/RETROSPECTIVE_SPEC.md。
  当用户提到：更新 put_in_practice / 复盘这一步 / 补实践笔记 / 更新 PhaseA 复盘 /
  学习总结文档 / 面试复习稿 时使用。
---

# Put-in-practice 复盘更新

用户用 `docs/put_in_practice/` 记录开发过程中的学习与面试复习材料。

## 结构规范（先读）

**强制遵守：** [`docs/put_in_practice/RETROSPECTIVE_SPEC.md`](../../docs/put_in_practice/RETROSPECTIVE_SPEC.md)

范本：`PhaseB/03_phase_2_5_lifecycle_hooks.md`、`PhaseB/04_phase_3_retrieval_jobs.md`。

一级目录必须是：

1. 一句话定位  
1.5 完整流程（构建顺序 + 运行时/用户路径 + 数据落点等）  
2. 本阶段完成了什么（对照计划）  
3. 做法 · 影响 · 亮点 · 踩坑（3.1–3.4）  
4. 本阶段知识点（**五问模板**逐条展开 + 速记对照表）  
5. 下一步  
6. 30 秒速记卡  

新建时复制规范文内「可复制骨架」；增量时只查漏补缺。

## 何时启用

用户说类似：

- 「更新 put_in_practice」
- 「复盘一下刚做完的 prompts / A1 / A2」
- 「把这一步补进 `01_phase_a1_skill_prototype.md`」
- 「查漏补缺实践笔记」
- 「按 RETROSPECTIVE_SPEC 写 / 加厚复盘」

## 硬性规则

1. **先读规范再写**：打开 `RETROSPECTIVE_SPEC.md` + 目标文档，再改。
2. **查漏补缺，禁止整篇重写**：已有正确内容保留；只增补缺失、修正过时状态表、追加新坑/新知识点。
3. **一次只服务「本步」**：以用户点名的完成项为准；不要把未做的 Phase 写成已完成。
4. **语气**：面向**面试复习**；知识点必须用五问体（是什么 / 为什么用 / 为什么不用替代 / 优缺点 / 为什么不用竞品）。
5. **路径约定**：
   - Phase A → `docs/put_in_practice/PhaseA/`
   - Phase B → `docs/put_in_practice/PhaseB/`
   - 文件名：`{序号}_phase_{id}_{短名}.md`
   - 新建文档前先问用户确认文件名；默认优先改已有文档。
6. **踩坑只增不删**：新坑用「坑 N」追加。

## 每次更新必须覆盖的三块

| 块 | 落点 |
|----|------|
| ① 完成了什么 | §2 状态表 + 交付物路径 + 验收边界 |
| ② 做法 · 影响 · 亮点 · 踩坑 | §3.1–3.4 |
| ③ 面试知识点 | §4 五问 + 速记对照表 |

细节与骨架见规范全文，此处不重复维护第二份模板。

## 操作流程（Agent 逐步执行）

```text
1. 读 RETROSPECTIVE_SPEC.md
2. 确认本步范围 + 目标文档路径
3. 读目标文档；列出「已有 / 缺失 / 过时」
4. 新建 → 复制规范骨架填满；增量 → 按规范查漏补缺
5. 改完用 3–5 条 bullet 汇报改了哪些节
```

## 与对话上下文

优先使用**当前对话**里刚发生的决策与文件变更；必要时用 git diff / 读相关源文件核对，避免复盘与代码脱节。
