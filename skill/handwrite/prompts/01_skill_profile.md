# 提示词 1: 【短标题，如「技能画像汇总」】

> **输入:** 【例如：`git_stats.py` 输出的 `stats.json`（可含 README 摘录）】  
> **输出:** 【例如：结构化技能画像 JSON + 概括性职位类型列表】

对应 `SKILL.md` Step 2。填完后删掉所有【】与本段说明。  
参考（只读）：`../prompts/01_skill_profile.md`（若存在 AI 稿）或 `SKILL.md` Step 2。

## 角色

【一句话】模型以什么身份产出画像？（如：技术招聘顾问 / 职业规划教练）

## 硬性规则

【至少 5 条，必须可执行】建议覆盖并写成「必须/禁止」：

1. 只能用统计里的事实；禁止臆测未出现的技术（除非依赖/路径有证据）
2. 论断如何标注证据（写出格式，如 `(来源: 仓库 / 字段)`）
3. `author_share` 低时如何描述个人贡献（禁止把团队成果写成人人功劳）
4. 主方向如何从「语言占比 + 依赖 + 目录 + commit 主题」推断
5. 语言占比必须引用哪个字段（如 `summary.overall_language_share`），禁止口算
6. 【可选】与用户口述冲突时怎么办（应与 `SKILL.md` 事实清单一致）

## 输出格式

【给出完整 JSON 骨架】建议字段与 `SKILL.md` Step 2 对齐：

- `primary_direction` / `secondary_directions`
- `languages`（name / share / evidence）
- `domains` / `tech_stack`
- `highlights_pool`（repo / claim / evidence）— 每仓最好能支撑 4–5 条贡献要点
- `project_one_liners`（每仓一句话产品/业务总结）
- `gaps_or_cautions`
- `job_direction_suggestions`（4–8 个**概括性** title + 一句话匹配理由；**不要**公司名、**不要**真实 JD）

```json
{
  "TODO": "把上面字段写成合法 JSON 示例（可用占位字符串）"
}
```

## 展示给用户的内容（HITL）

【列出】确认前必须展示哪些块？（技术栈 / 主方向 / 贡献要点 / 一句话总结 / 概括职位类型 …）

## 与用户的交互

【写清】

- 必须请用户确认哪些字段？（至少：主方向、gaps）
- **未确认能否进入 Step 3？**
- 用户修改画像后如何更新输出？

<!--
自检：
- [ ] 输入输出写清
- [ ] 防幻觉规则具体可执行
- [ ] JSON 可被后续 Step 3/4 消费
- [ ] job_direction_suggestions 明确「不联网、非真实岗位」
- [ ] 未要求编造统计中不存在的数字
-->
