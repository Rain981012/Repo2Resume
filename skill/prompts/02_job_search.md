# 提示词 2:职位搜索与匹配打分

> 输入:确认后的技能画像。输出:带评分的职位推荐列表。

## 第一步:生成搜索词

根据画像的 `primary_direction` 和 `tech_stack` 生成 3-5 组网络搜索词,并与用户确认目标城市/远程偏好、级别(初级/中级/高级)。搜索词示例:

- "Python 后端工程师 招聘 {城市} {当年}"
- "FastAPI backend engineer hiring remote"
- "{tech_stack 中最突出的 2 项} 开发工程师 岗位"

`secondary_directions` 至多生成 1 组搜索词——推荐的优先级应反映画像占比。

## 第二步:搜索并提取 JD

对每组搜索词执行网络搜索,提取真实在招职位。每个职位记录:职位名、公司、地点、来源链接、职责摘要、技能要求列表。找不到完整 JD 的职位丢弃。目标:至少 5 个可评分职位。用户手动粘贴的 JD 一并纳入。

## 第三步:匹配打分

对每个职位输出:

```json
{
  "title": "...", "company": "...", "url": "...",
  "score": 8,
  "matched_skills": ["职位要求 Python/FastAPI,画像 share 0.82"],
  "missing_skills": ["要求 Kafka,统计中无证据"],
  "verdict": "一句话:为什么该投/不该投"
}
```

**打分规则(1-10):**

- 职位核心要求与 `primary_direction` 一致:基线 7 分,再按 tech_stack 重合度加减
- 职位核心要求属于 `secondary_directions`:基线 5 分
- 核心要求画像中无任何证据:≤ 3 分,不推荐
- `missing_skills` 必须诚实列出,禁止为了凑高分忽略缺口

按分数降序展示,请用户选定 1 个目标职位(或多选)后进入简历生成。
