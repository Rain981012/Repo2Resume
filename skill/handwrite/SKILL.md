---
name: repo2resume
description: >
  【在此写 1–3 句】第三人称。必须同时包含：
  WHAT = 从本地 git 贡献 → 技能画像 → 搜匹配职位 → 生成可溯源 Markdown 简历
  WHEN = 用户提到：生成简历 / resume / CV / 技能画像 / 找匹配工作 / 分析技术栈方向 …
  （≤1024 字，不要用「我可以帮你」这种第一人称）
  【1. sada
  2. asdsa】
---

<!--
填写说明：标题栏「建议用」可直接采用，或改成同义短句（中英均可）。
正文【】里是你要写的内容，不要留空 TODO。
参考成品只读：../SKILL.md —— 卡住再说「看参考版并附理解题」。
-->

# 【建议标题】Repo2Resume：从本地代码到定制简历

【写 2–3 句】这个 skill 做什么。  
【再写 1 句】阶段 A **明确不做**：独立 CLI harness、向量库/RAG 运行时、自研多智能体框架（那些是阶段 B）。

## 进度清单

把下面「…」换成与各 Step 标题一致的短标签（复制建议标题即可）：

```
- [ ] Step 1 分析仓库 → 统计 JSON
- [ ] Step 2 技能画像 ⏸ 用户确认
- [ ] Step 3 职位搜索与打分 ⏸ 用户选定目标
- [ ] Step 4 生成简历草稿 → 自我审稿
- [ ] Step 5 与用户迭代修改 ⏸ 确认后导出
```

（若你改了 Step 标题，这里同步改。）

## Step 1: 【建议标题】分析仓库

【问用户】仓库路径有哪些？git 作者名/邮箱是什么（可多个）？不确定时是否先 `git shortlog -sne`？

【执行】写出完整命令模板（路径用相对 skill 的 `scripts/git_stats.py`）：

```bash
python3 scripts/git_stats.py <repo1> <repo2> ... --author <email或名字> --output runs/<YYYY-MM-DD>/stats.json
```

【异常】`errors` / `warning` / `author_commits == 0` 时你让 agent 怎么做？

## Step 2: 【建议标题】技能画像 ⏸

【读】`prompts/01_skill_profile.md`  
【输入】Step 1 的 `stats.json`  
【输出】结构化画像（字段由 01 提示词定义）

【⏸】向用户展示并确认哪些内容？（至少：主方向、gaps）——未确认不得进 Step 3。

## Step 3: 【建议标题】职位搜索与打分 ⏸

【读】`prompts/02_job_search.md`  
【先问】城市 / 远程 / 职级偏好  
【做】按 02 生成搜索词 → 联网搜真实在招职位（建议 ≥5）→ 打分排序；也接受用户粘贴 JD

【⏸】用户选定 **1 个（或多选）目标职位** 后才进 Step 4。

## Step 4: 【建议标题】生成简历草稿 + 自我审稿

【读】`prompts/03_resume_writing.md`，套用 `templates/resume.md.j2` 结构  
【问】姓名、联系方式、教育、工作经历（统计里没有）  
【然后】按 `prompts/04_critic_review.md` 自查；写清最多几轮修订（建议 ≤2）

## Step 5: 【建议标题】迭代修改与导出 ⏸

【做】展示草稿 Markdown，按用户意见改；新数字必须来自 `stats.json` 或用户亲口提供  
【⏸】用户说定稿后再写文件，建议路径：`runs/<YYYY-MM-DD>/resume_<公司或职位>.md`  
【问】投递前是否删除 `<!-- src -->` 溯源注释？

## 归档

【列出】本轮至少保存哪些文件到 `runs/<日期>/`  
【写出】`meta.json` 最小字段（至少：`model`, `date`, `target_job`）

## 事实清单规则（全程最高优先级）

【用自己的话写 4–6 条】建议每条一行「必须/禁止」。至少覆盖：

1. `stats.json` vs 用户口述：谁算事实来源  
2. 禁止编造：用户量、性能%、营收、团队规模等  
3. 项目 bullet 必须带 `<!-- src: … -->`  
4. 多人仓库如何用 `author_share`  
5. 拿不准时：问用户，不擅自美化

<!--
自检：
- [ ] description 第三人称 + WHAT + WHEN
- [ ] 五步标题已定，进度清单与之一致
- [ ] 三个 ⏸：画像确认 / 选定职位 / 定稿导出
- [ ] 有 git_stats.py 命令
- [ ] 有归档 + 事实清单
- [ ] 未写 CLI/Redis/向量库实现
-->
