# 提示词 1: TODO 短标题

> 输入: TODO（例如 git_stats JSON）  
> 输出: TODO（例如结构化技能画像）

## 角色

TODO: 你希望模型以什么身份写画像？（一句话）

## 硬性规则

TODO: 列出至少 4 条，建议覆盖：

1. 只能用统计里的事实 / 禁止臆测未出现的技术
2. 论断如何标注证据（格式你定）
3. `author_share` 低时如何描述贡献
4. 领域/主方向如何从语言 + 依赖 + commit 主题推断
5. 语言占比引用哪个字段，禁止自行估算

## 输出格式

TODO: 给出你要求的 JSON（或 Markdown）骨架。建议字段含：

- primary_direction / secondary_directions
- languages（含 share + evidence）
- domains / tech_stack
- highlights_pool（repo + claim + evidence）
- gaps_or_cautions

```json
{
  "TODO": "填你的 schema"
}
```

## 与用户的交互

TODO: 输出后要让用户确认哪些条目？什么情况下必须停下改画像再继续？

<!--
自检：
- [ ] 输入输出写清
- [ ] 防幻觉规则具体可执行
- [ ] 输出可被程序/后续步骤消费
- [ ] 未要求模型编造统计中不存在的数字
-->
