# 提示词 3:亮点挑选与简历写作

> 输入:技能画像 + 目标职位 JD + git 统计(事实清单)。输出:按模板填写的简历 Markdown。

## 第一步:挑选亮点

从画像的 `highlights_pool` 和各仓库统计中,挑选与目标 JD 最相关的 2-4 个项目。选择标准:

1. 技术栈与 JD 要求重合度高的优先
2. `author_share` 高(个人主导)的优先
3. commit 量大、活跃周期长的优先
4. 宁缺毋滥:不相关的项目不硬凑

## 第二步:写作规则

**事实约束(最高优先级):**

- 每条 bullet 末尾用 HTML 注释标注来源:`<!-- src: 仓库名, 依据 -->`(导出时保留,方便核查)
- 数字只能来自统计 JSON:commit 数、语言占比、活跃月数、文件数。**禁止编造**用户数、性能提升百分比、营收影响——统计里没有这些
- 想强调成果但缺数字时,询问用户补充真实数字,不要虚构占位

**风格:**

- 每个项目 3-5 条 bullet,每条 ≤ 2 行
- 动词开头(设计/实现/重构/优化/搭建),点明技术方案与规模
- 自然嵌入 JD 中的关键词(ATS 友好),但仅限画像中有证据的技能
- 与 JD 无关的技术细节压缩或删除

## Few-shot 示例

**输入统计:** repo `order-service`,author_commits 156,author_share 0.61,Python 0.9,依赖含 fastapi/sqlalchemy/redis/celery,commit 主题多为 "add order api" "fix race condition in payment callback" "add celery retry"
**目标 JD:** 后端工程师,要求 Python、高并发、消息队列

**好的输出:**

> - 主导订单服务后端开发(156 commits,占项目 61%),基于 FastAPI + SQLAlchemy 设计订单与支付回调 API <!-- src: order-service, author_commits/dependencies -->
> - 使用 Celery + Redis 实现异步任务队列,为支付回调增加重试与幂等处理,修复回调竞态问题 <!-- src: order-service, commit subjects "fix race condition"/"add celery retry" -->

**坏的输出(违规示例,禁止):**

> - 支撑日均百万订单,接口 P99 延迟降低 40% ← 统计中不存在的数字
> - 精通微服务架构与分布式系统 ← 空话,无证据

## 第三步:填模板

用 `templates/resume.md.j2` 的结构组织全文。个人信息(姓名/联系方式/教育/工作经历)统计里没有,向用户询问后填入;用户不提供的部分留占位符。
