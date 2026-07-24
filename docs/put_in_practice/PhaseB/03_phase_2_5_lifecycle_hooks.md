# Phase B / Phase 2.5 实践复盘：Lifecycle Hooks（面试复习稿）

> 项目：Repo2Resume
> 阶段：Phase 2.5 — Harness 加固（lifecycle hooks）
> 对应：`MVP_PLAN.md` Phase 2.5；前置 `02_phase_2_harness.md`

## 1. 一句话定位

Phase 2 把 agent 的「心脏」（loop）和「四肢」（tools）手写完了，但 `ToolRegistry.call` 只会「调工具、拿结果」一件事。Phase 2.5 给 `call` 装上**可插拔的 before/after 钩子**，把权限、观测、错误恢复三个**横切关注点**从核心流程里剥离出去——加关注点不再需要改 `call`，只挂一个 hook。

## 2. 本阶段完成了什么（对照计划）

| Step | 交付物 | 状态 |
|------|--------|------|
| 1 | `Hook` Protocol + `ToolRegistry.add_hook` + `call` 跑 before/after | ✅ |
| 2 | `call` 重写：try/except + after 能见异常 + 能吞异常 | ✅ |
| 3 | `PermissionHook`（权限分级）+ 重构（`Tool.risk` 事实/策略分离） | ✅ |
| 4 | `TraceHook`（观测）+ `tool_traces` 表（迁移 v2）+ `/cost` 命令 | ✅ |
| 5 | `ErrorRecoveryHook`（错误恢复，选择性吞异常） | ✅ |

交付物路径：

- `src/repo2resume/agent/tools.py` — `Hook` Protocol、`Tool.risk` 字段、`ToolRegistry.add_hook` / `call` 重写
- `src/repo2resume/agent/hooks.py` — `PermissionDenied` / `PermissionHook` / `TraceHook` / `ErrorRecoveryHook`
- `src/repo2resume/storage/db.py` — 迁移 v2、`record_tool_trace` / `tool_trace_stats`
- `src/repo2resume/cli.py` — 三个 hook 接线、`/cost` REPL 子命令
- `tests/test_agent_hooks.py` / `tests/test_permission_hook.py` / `tests/test_trace_hook.py` / `tests/test_error_recovery_hook.py`

验收：25 个 hook 相关测试全绿；chat 真跑 `/cost` 表正确显示 `analyze_repo` 1 次 / 72ms / 0 errors。

## 3. 做法 · 影响 · 亮点 · 踩坑

### 3.1 做了什么

- 在 `ToolRegistry.call` 前后插 before/after 链，把「调工具」包成「before → tool → after」三段。
- 重写 `call`：`try` 捕获工具异常 → `error` 传给每个 after → after 返回非 None 且 error 非空则吞掉异常 → 末尾仍非空才 re-raise。
- 写 `PermissionHook`：基于工具 `risk` 等级决定放行/确认/拒绝，拒绝抛 `PermissionDenied`。
- 重构 `PermissionHook`：把「哪些工具要确认」从硬编码名单改成 `Tool.risk`（事实）+ `needs_confirm_risks` 集合（策略）。
- 写 `TraceHook`：栈式 `perf_counter` 计延迟，写 `tool_traces` 表；加 `/cost` 命令按 session 聚合展示。
- 写 `ErrorRecoveryHook`：工具异常转文本回填 LLM，`PermissionDenied` 不吞。

### 3.2 对项目的影响

- `call` 不再随关注点膨胀——加权限/观测/恢复只挂 hook，核心循环零改动。
- agent 能自纠错：工具失败不再一炸中断对话，而是变成文本喂回 LLM，LLM 换参数/换工具/告诉用户。
- 运行时可观测：`/cost` 让用户看到「这个 session 调了哪些工具、多快、有没有错」，agent 不再是黑盒。
- 权限可演进：加 `write`/`network` 工具时，工具自声明 `risk`，权限策略零改动。

### 3.3 亮点（可上面试）

1. **事实/策略分离**：`Tool.risk` 是「我是什么」（事实），`PermissionHook` 是「这种风险怎么办」（策略）。加新工具只动事实，加新风险等级只动策略，两者解耦。
2. **观测先于恢复**：hook 注册顺序 `Permission → Trace → ErrorRecovery`，保证 `TraceHook.after` 在 `ErrorRecoveryHook.after` 之前跑——失败被吞之前先记进 trace，否则 `/cost` 永远显示 0 errors。
3. **选择性吞异常**：`after` 能吞异常，但 `PermissionDenied` 不吞——用户拒绝（永久）≠ 工具 bug（可重试），错误分类决定是否恢复。
4. **栈式传值**：Hook 协议没有 before→after 的显式数据通道，用 `self._starts: list[float]` 栈传起始时间，单线程 loop 下严格配对。

### 3.4 踩坑

**坑 1：Hook Protocol 方法自递归**
- 现象：`Hook.before` 写成 `return self.before(...)`，一调就栈溢出。
- 教训：Protocol 方法体应是抽象 stub（`...`），不是调自身。
- 面试升华：Protocol 是结构化类型，方法体只是占位，实现由具体类提供。

**坑 2：`call` 里 `error` 未初始化 → `UnboundLocalError`**
- 现象：成功路径下 `after` 引用 `error`，但 `error` 只在 `except` 里赋值，成功路径未定义。
- 教训：`try` 块里先 `error = None`，再 `result = tool.call()`。Python 作用域里 `except` 赋值的变量在 `try` 成功时不存在。
- 面试升华：Python 没有「块作用域」，但「赋值发生在哪条路径」决定变量是否绑定。

**坑 3：吞异常条件写反**
- 现象：写成 `if error is None and new is not None:`，正好和想要的相反；且漏了 `result = new` 导致 after 返回值被丢。
- 教训：吞异常的语义是「有错 + after 给了替代值」→ `if error is not None and new is not None:`，然后 `error = None; result = new`。
- 面试升华：先写语义再写条件，别凭感觉写 `is None`。

**坑 4：测试 fixture 用 `BaseModel` 基类 → `PermissionDenied` 没机会抛**
- 现象：`test_error_recovery_does_not_swallow_permission_denied` 一直「DID NOT RAISE」。
- 根因：fixture 用 `params_model=BaseModel`（基类本身），`Tool.call` 做 `BaseModel.model_validate({})` 直接抛 `PydanticUserError: BaseModel cannot be instantiated directly`——handler 根本没执行，`PermissionDenied` 没机会抛，被 `ErrorRecoveryHook` 当普通异常吞了。
- 教训：`params_model` 必须是 `BaseModel` 的**子类**，不能是基类本身。用一个空子类 `class _NoParams(BaseModel): pass`。
- 面试升华：测试红的时候，先确认「被测代码」和「测试夹具」哪个错了——这次是夹具把被测路径短路了。

**坑 5：litellm `zai` provider 默认打国际域名 `api.z.ai`，国内需代理 → `Connection error`**
- 现象：chat 真跑报 `litellm.InternalServerError: ZaiException - Connection error`，重试 3 次全败。
- 排查链：开 litellm debug → 异常链最底层是 `ProxyError` → 环境有 `HTTP_PROXY/ALL_PROXY=127.0.0.1:55272`（沙箱/本机代理）→ litellm `zai` provider 默认 `ZAI_API_BASE=https://api.z.ai/api/paas/v4`（国际域名）→ 代理转发失败。
- 验证：`curl https://open.bigmodel.cn`（国内端点）直连 401（活着）；设 `ZAI_API_BASE=https://open.bigmodel.cn/api/paas/v4` 后 litellm 调用成功返回「在吗」。
- 教训：`Connection error` 不一定是网络断了，先开 debug 看异常链最底层（`ProxyError` 才是真因）；provider 的默认 base_url 可能不适合你的网络环境。
- 面试升华：分层排障——应用层（litellm 包装）→ 客户端层（openai APIConnectionError）→ 传输层（ProxyError）；越往下越接近真相。
- 待办（可选加固）：给 `AppConfig` 加 `llm_api_base` 字段，`_api_kwargs` 里传 `api_base`，把国内端点写进 `config.toml`，摆脱环境变量。

## 4. 本阶段知识点（面试向）

| 概念 | 在本项目里怎么练到的 | 可背的一句话 |
|------|----------------------|-------------|
| Lifecycle Hooks | `ToolRegistry.call` 前后挂 before/after 链 | 在固定点执行的可插拔回调，把横切关注点从主流程剥离，主流程不认识具体 hook。 |
| 控制反转（IoC） | `call` 不知有哪些 hook，hook 自己 `add_hook` 注册 | 主流程放弃对扩展的控制权，由外部组件注册自己，符合开闭原则。 |
| Hook Protocol（结构化类型） | `Hook` 用 `Protocol` 定义，`PermissionHook` 等不需要继承 | Protocol 是鸭子类型的静态版，有指定方法签名就算实现，不要求显式继承。 |
| 事实/策略分离 | `Tool.risk`（事实）vs `PermissionHook.needs_confirm_risks`（策略） | 工具自声明属性是「事实」，基于属性做决策是「策略」，分开则各自可独立演进。 |
| Observability | `TraceHook` 写 `tool_traces` + `/cost` 展示 | 让运行时内部状态可见（log/metric/trace），不改控制流，用于监控与排障。 |
| 选择性错误恢复 | `ErrorRecoveryHook` 吞工具异常但不吞 `PermissionDenied` | 能恢复 ≠ 该恢复；瞬时故障（可重试）和永久拒绝（不可重试）要分类对待。 |
| ReAct 错误闭环 | 工具失败 → 文本回填 → LLM 自纠错 | 观察（工具结果）即使失败也喂回 LLM，agent 在「想-调-看」循环里自纠错，不中断。 |
| 异常分类与防御性编程 | `since="null"` 清洗 + `_parse_since` try/except + `PermissionDenied` 守卫 | 对概率性输入（LLM/网络）做边界清洗和异常分类，用确定性代码兜住。 |

### 面试问答速记

- **Q：你的 agent 工具调用失败了怎么办？**
  A：挂了 `ErrorRecoveryHook`，工具异常在 `after` 里被吞成一段 `[工具 X 执行失败: 原因]` 文本，作为 tool 结果回填给 LLM，LLM 看到后自己换参数或换工具重试。但 `PermissionDenied` 不吞——用户拒绝是不可重试的，必须抛出去给用户看。

- **Q：hooks 之间有顺序依赖吗？**
  A：有。注册顺序就是执行顺序。`Permission → Trace → ErrorRecovery`：权限在 before 阶段拒绝就直接 raise，不进 after；Trace 必须在 ErrorRecovery 之前跑 after，否则失败被吞后 trace 记不到 error。

- **Q：为什么 `Tool.risk` 而不是 `PermissionHook` 里写死工具名？**
  A：事实/策略分离。工具自声明「我是什么风险」是事实，`PermissionHook` 决定「这种风险要不要问用户」是策略。加新工具只动 `Tool(risk=...)`，加新风险等级只动 `needs_confirm_risks` 集合，两边解耦，不会改一边漏另一边。

## 5. 下一步

1. **commit Phase 2.5 全部成果**（Step 3 重构 + Step 4 + Step 5）。
2. **（可选）`llm_api_base` 配置加固**：把国内端点写进 `config.toml`，摆脱 `ZAI_API_BASE` 环境变量。
3. **compaction 升级**：从「按条裁」改成「按完整轮次裁」，避免拆散 tool_call ↔ tool 结果配对。
4. **进入 Phase 3**：职位搜索 + 检索（Tavily / ChromaDB / BM25+向量混合）。

---

## 6. 30 秒速记卡

- **做了什么：** 给 `ToolRegistry.call` 装可插拔 before/after 钩子，落地权限/观测/错误恢复三个 hook。
- **Gate：** 25 个 hook 测试绿；chat 真跑 `/cost` 表正确；agent 工具失败能自纠错。
- **一句话：** hook = 横切关注点外挂；注册顺序即执行顺序；观测先于恢复；能吞 ≠ 该吞。
- **踩坑：** Protocol 自递归；`error` 未初始化；吞异常条件写反；fixture 用 `BaseModel` 基类短路了被测路径；litellm `zai` 默认打 `api.z.ai` 需代理。
- **下一站：** commit → Phase 3 检索。
