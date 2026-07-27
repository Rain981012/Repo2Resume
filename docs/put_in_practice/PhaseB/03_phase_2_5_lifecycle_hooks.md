# Phase B / Phase 2.5 实践复盘：Lifecycle Hooks（面试复习稿）

> 项目：Repo2Resume
> 阶段：Phase 2.5 — Harness 加固（lifecycle hooks）
> 对应：`MVP_PLAN.md` Phase 2.5；前置 `02_phase_2_harness.md`

## 1. 一句话定位

Phase 2 把 agent 的「心脏」（loop）和「四肢」（tools）手写完了，但 `ToolRegistry.call` 只会「调工具、拿结果」一件事。Phase 2.5 给 `call` 装上**可插拔的 before/after 钩子**，把权限、观测、错误恢复三个**横切关注点**从核心流程里剥离出去——加关注点不再需要改 `call`，只挂一个 hook。

## 1.5 Phase 2.5 的完整流程

### A. 构建 / 注册顺序（为什么这样挂）

```
① tools.py
   Hook Protocol（before / after）
   Tool.risk = readonly | write | network   ← 事实
   ToolRegistry.add_hook / call 重写
      │
② hooks.py（具体实现，互不认识）
   PermissionHook  → TraceHook → ErrorRecoveryHook
   （注册顺序 = 执行顺序）
      │
③ db 迁移 v2：tool_traces + record_tool_trace / tool_trace_stats
      │
④ cli chat 接线
   reg.add_hook(Permission…)
   reg.add_hook(Trace…)          ← session_id 确定之后
   reg.add_hook(ErrorRecovery…)
   REPL 增加 /cost
      │
⑤ loop.py（Phase 2.5 卡死检测）
   连续同名工具调用 ≥ max_repeated_tool → 停止
```

**为什么 Permission → Trace → ErrorRecovery？**  

- Permission 在 before 拒绝直接 raise，不必留下半截 trace 栈。  
- Trace 的 after 必须在 ErrorRecovery **之前**：先记下 error，再被吞成文本；顺序反了 `/cost` 永远 0 errors。  
- ErrorRecovery 最后吞「可恢复」异常，把文本当 tool 结果回填。

### B. 运行时：一次 `registry.call(name, args)`

```
registry.call("analyze_repo", args)
  │
  ├─ for h in hooks:  h.before(name, args)     # 可改 args；Permission 可 raise
  │
  ├─ try:
  │     result = tool.call(args)               # Pydantic 校验 → handler
  │     error = None
  │   except Exception as e:
  │     result = None
  │     error = e
  │
  ├─ for h in hooks:  new = h.after(name, args, result, error)
  │     若 error 且 new 非 None → 吞掉：error=None, result=new
  │     否则 result = new（成功路径也可改写结果）
  │
  └─ 若 error 仍在 → re-raise
      否则 return result → loop 回填进 context → 下一轮 LLM
```

### C. 和 AgentLoop 的关系（端到端）

```
loop.run(user)
  └─ 每轮 LLM 若要调工具:
        registry.call(...)     ← 上面 B；hooks 全在这里发生
        卡死检测：同名连续 ≥ N → 直接 return 提示
        continue → 再调 LLM（若 ErrorRecovery 回填了失败文本，模型可自纠）
```

**两条恢复路径别混：**  

- **工具抛异常** → ErrorRecoveryHook 转文本（仍算一次成功的 tool 结果回填）。  
- **工具「成功」但业务失败、模型反复重试** → 卡死检测（只看工具名）可能误杀——Phase 3 复盘里有踩坑。

### D. 数据落在哪

| 数据           | 位置                                     |
| ------------ | -------------------------------------- |
| 每次工具调用 trace | SQLite `tool_traces`                   |
| `/cost` 聚合   | `tool_trace_stats(session_id)`         |
| 权限策略         | `needs_confirm_risks` + 交互 `confirm()` |
| 工具风险事实       | `Tool.risk` 字段                         |

---

## 2. 本阶段完成了什么（对照计划）

| Step | 交付物                                                               | 状态  |
| ---- | ----------------------------------------------------------------- | --- |
| 1    | `Hook` Protocol + `ToolRegistry.add_hook` + `call` 跑 before/after | ✅   |
| 2    | `call` 重写：try/except + after 能见异常 + 能吞异常                          | ✅   |
| 3    | `PermissionHook`（权限分级）+ 重构（`Tool.risk` 事实/策略分离）                   | ✅   |
| 4    | `TraceHook`（观测）+ `tool_traces` 表（迁移 v2）+ `/cost` 命令               | ✅   |
| 5    | `ErrorRecoveryHook`（错误恢复，选择性吞异常）                                  | ✅   |

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

**坑 2：**`call` **里** `error` **未初始化 →** `UnboundLocalError`

- 现象：成功路径下 `after` 引用 `error`，但 `error` 只在 `except` 里赋值，成功路径未定义。
- 教训：`try` 块里先 `error = None`，再 `result = tool.call()`。Python 作用域里 `except` 赋值的变量在 `try` 成功时不存在。
- 面试升华：Python 没有「块作用域」，但「赋值发生在哪条路径」决定变量是否绑定。

**坑 3：吞异常条件写反**

- 现象：写成 `if error is None and new is not None:`，正好和想要的相反；且漏了 `result = new` 导致 after 返回值被丢。
- 教训：吞异常的语义是「有错 + after 给了替代值」→ `if error is not None and new is not None:`，然后 `error = None; result = new`。
- 面试升华：先写语义再写条件，别凭感觉写 `is None`。

**坑 4：测试 fixture 用** `BaseModel` **基类 →** `PermissionDenied` **没机会抛**

- 现象：`test_error_recovery_does_not_swallow_permission_denied` 一直「DID NOT RAISE」。
- 根因：fixture 用 `params_model=BaseModel`（基类本身），`Tool.call` 做 `BaseModel.model_validate({})` 直接抛 `PydanticUserError: BaseModel cannot be instantiated directly`——handler 根本没执行，`PermissionDenied` 没机会抛，被 `ErrorRecoveryHook` 当普通异常吞了。
- 教训：`params_model` 必须是 `BaseModel` 的**子类**，不能是基类本身。用一个空子类 `class _NoParams(BaseModel): pass`。
- 面试升华：测试红的时候，先确认「被测代码」和「测试夹具」哪个错了——这次是夹具把被测路径短路了。

**坑 5：litellm** `zai` **provider 默认打国际域名** `api.z.ai`**，国内需代理 →** `Connection error`

- 现象：chat 真跑报 `litellm.InternalServerError: ZaiException - Connection error`，重试 3 次全败。
- 排查链：开 litellm debug → 异常链最底层是 `ProxyError` → 环境有 `HTTP_PROXY/ALL_PROXY=127.0.0.1:55272`（沙箱/本机代理）→ litellm `zai` provider 默认 `ZAI_API_BASE=https://api.z.ai/api/paas/v4`（国际域名）→ 代理转发失败。
- 验证：`curl https://open.bigmodel.cn`（国内端点）直连 401（活着）；设 `ZAI_API_BASE=https://open.bigmodel.cn/api/paas/v4` 后 litellm 调用成功返回「在吗」。
- 教训：`Connection error` 不一定是网络断了，先开 debug 看异常链最底层（`ProxyError` 才是真因）；provider 的默认 base_url 可能不适合你的网络环境。
- 面试升华：分层排障——应用层（litellm 包装）→ 客户端层（openai APIConnectionError）→ 传输层（ProxyError）；越往下越接近真相。
- 待办（可选加固）：给 `AppConfig` 加 `llm_api_base` 字段，`_api_kwargs` 里传 `api_base`，把国内端点写进 `config.toml`，摆脱环境变量。

## 4. 本阶段知识点（面试向 · 五问模板）

与 Phase 3 同标准：

1. **X 是什么？**
2. **为什么要在这个项目用它？**
3. **为什么不用……？**
4. **有什么优缺点？**
5. **为什么不用其他同类技术？**

---

### ① Lifecycle Hooks（生命周期钩子）

**1. 是什么？**  
在 `ToolRegistry.call` 前后固定时机执行的可插拔回调：`before(name, args)` 可改参/中止；`after(name, args, result, error)` 可改结果/吞异常。

**2. 为什么要在这个项目用它？**  
权限、观测、错误恢复都是横切关注点。塞进 `call` 或 loop 会让核心膨胀、难测。Hook 让「加关注点 = add_hook」，符合开闭原则。

**3. 为什么不用「在 loop._execute_tools 里写死 if 权限 / if 记日志」？**  
每加一种关注点就改 loop；测试要造整段 loop；关注点互相缠在一起。

**4. 优缺点？**  

| 优点               | 缺点                 |
| ---------------- | ------------------ |
| 关注点分离、可组合        | 间接性：看 call 不知挂了谁   |
| 单一 chokepoint，不漏 | 顺序依赖要文档化；异常语义要约定清楚 |

**5. 为什么不用其他技术？**  

- **AOP / 装饰器包每个 handler：** 易漏新工具；不如 registry.call 一处强制。  
- **中间件框架（Starlette 式）：** 思想同构，但自研几行 Protocol 更贴学习目标。  
- **改 AgentLoop 内核塞逻辑：** 和「loop 只编排」冲突。

---

### ② Hook Protocol + 结构化类型

**1. 是什么？**  
用 `typing.Protocol` 定义 before/after 签名；`PermissionHook` 等**不需要继承**，有同名方法即满足。

**2. 为什么要在这个项目用它？**  
解耦「框架契约」与「具体 hook」；hooks.py 不强迫继承 tools.py 里的基类，避免循环依赖。

**3. 为什么不用 ABC 强制继承？**  
继承层级变重；测试假 hook 也要继承。Protocol 更轻、更符合鸭子类型。

**4. 优缺点？**  

| 优点     | 缺点                  |
| ------ | ------------------- |
| 零侵入实现  | 方法体若误写成自调用会栈溢出（踩过）  |
| 静态检查友好 | 运行时不强制「必须实现」，靠约定与测试 |

**5. 为什么不用其他技术？**  

- **回调函数列表而非对象：** 传 before/after 两套 list 也行，但对象更好持有状态（如 TraceHook 的 `_starts` 栈）。  
- **插件 entry_points：** 产品化可做，MVP 显式 add_hook 更清晰。

---

### ③ 事实 / 策略分离（Tool.risk vs PermissionHook）

**1. 是什么？**  

- **事实：** 工具自声明 `risk=readonly|write|network`。  
- **策略：** `PermissionHook` 用 `needs_confirm_risks` + `confirm()` 决定要不要问用户。

**2. 为什么要在这个项目用它？**  
加新工具只标 risk；加新策略只改集合。两边解耦，不会「权限表忘了写新工具名」。

**3. 为什么不用「PermissionHook 里写死工具名黑名单」？**  
每加工具改权限模块；工具与策略耦合；测试要记一堆名字。

**4. 优缺点？**  

| 优点         | 缺点                      |
| ---------- | ----------------------- |
| 演进独立；好测    | 工具作者必须认真标 risk（标错则策略误判） |
| 与「策略可配置」一致 | 细粒度（按参数敏感度）还要扩展         |

**5. 为什么不用其他技术？**  

- **OS 级沙箱 / seccomp：** 更强但过重，CLI MVP 不合适。  
- **每次工具都人工确认：** UX 极差；只读应放行。

---

### ④ TraceHook + Observability（/cost）

**1. 是什么？**  
观测：before 压入时间戳，after 算延迟并写入 `tool_traces`；`/cost` 按 session 聚合次数/耗时/错误。

**2. 为什么要在这个项目用它？**  
Agent 易黑盒。要知道调了啥、多久、是否失败——排障与面试演示都需要。观测**不改控制流**。

**3. 为什么不用「只 print 日志到终端」？**  
难聚合、难按 session 查、关掉终端就没了。落库可 `/cost` 复盘。

**4. 优缺点？**  

| 优点       | 缺点                      |
| -------- | ----------------------- |
| 可聚合、可回归看 | 多写库；要保证 before/after 配对 |
| 与恢复解耦    | 注册顺序错会导致 error 统计不准     |

**5. 为什么不用其他技术？**  

- **OpenTelemetry 全套：** 正确方向，MVP 用 SQLite 够学「可观测性」概念。  
- **只记 LLM token 不计工具：** 工具延迟往往是大头（挖仓/embedding），必须记。

---

### ⑤ ErrorRecoveryHook（选择性吞异常）

**1. 是什么？**  
工具异常在 after 里转成 `[工具 X 执行失败: …]` 文本返回，call 不再抛出 → loop 当普通 tool 结果回填 → LLM 可自纠。**PermissionDenied 不吞**。

**2. 为什么要在这个项目用它？**  
工具一炸就中断对话，体验差。把失败变成「观察」喂回模型，符合 ReAct 闭环。但用户拒绝执行不能偷偷重试。

**3. 为什么不用「loop 外层巨大 try/except 打印 traceback 结束」？**  
对话直接死；模型没机会换参；和 agent 理念不符。

**4. 优缺点？**  

| 优点              | 缺点                     |
| --------------- | ---------------------- |
| 自纠错、对话不中断       | 模型可能对同一失败工具死磕 → 触发卡死检测 |
| 错误分类（可恢复 vs 永久） | 吞错后若 Trace 顺序不对会丢观测    |

**5. 为什么不用其他技术？**  

- **自动换备用工具无提示：** 不可解释，危险。  
- **无限重试同一 call：** 无上限更糟；应回填 + max_rounds/卡死检测。  
- **所有异常包括 PermissionDenied 都吞：** 违反权限语义。

---

### ⑥ 控制反转（IoC）与开闭原则（合讲）

**1. 是什么？**  
`call` 不写死有哪些 hook；外部 `add_hook` 注册。对扩展开放、对修改关闭。

**2. 为什么要在这个项目用它？**  
Phase 2.5 三个能力、未来 FactSheetHook 等，都不该改 call 核心。

**3. 为什么不用「call 里 import 并写死三个 hook」？**  
测试难关；CLI/非 CLI 不同组合困难。

**4. 优缺点？** 间接性换灵活性——见 Hooks 条。

**5. 为什么不用完整 DI 框架？** 杀鸡用牛刀；显式 add_hook 足够。

---

### ⑦ 卡死检测（loop 侧，与 hook 互补）

**1. 是什么？**  
`AgentLoop` 内：连续同名工具调用 ≥ `max_repeated_tool` 则停止。

**2. 为什么要在这个项目用它？**  
ErrorRecovery 鼓励重试；模型也可能无意义反复调。max_rounds 是硬上限，卡死检测更早、更准（针对「同工具」）。

**3. 为什么不用「只靠 max_rounds」？**  
10 轮里可以同工具刷满，浪费 token 与时间；同名检测能更早停。

**4. 优缺点？**  

| 优点   | 缺点                                |
| ---- | --------------------------------- |
| 实现简单 | 只看工具名：失败重试 / 合法换参会被误杀（Phase 3 踩过） |

**5. 更好替代（可讲改进）？**  
签名含 arguments；或「连续失败且错误文本相似」才算卡死。

---

### 速记对照表

| 我用了              | 为什么不用常见替代     | 一句话          |
| ---------------- | ------------- | ------------ |
| call 上 Hooks     | loop 写死 if    | 横切外挂、OCP     |
| Protocol         | 强制继承 ABC      | 轻量结构化类型      |
| risk 事实/策略分离     | 工具名黑名单        | 两边独立演进       |
| Trace 落库 + /cost | 只 print       | 可聚合排障        |
| 选择性吞异常           | 一律崩溃或一律吞      | 可恢复 ≠ 该恢复    |
| 观测先于恢复的顺序        | 随意注册          | 否则 error 统计丢 |
| 卡死检测             | 只靠 max_rounds | 更早停同名死循环     |

## 5. 下一步

1. ~~进入 Phase 3~~ → 已完成，见 `04_phase_3_retrieval_jobs.md`。
2. **（可选）**`llm_api_base` **配置加固**：国内端点写进 `config.toml`。
3. **compaction 升级**：按完整轮次裁，避免拆散 tool_call ↔ tool 结果。
4. **卡死检测升级**（可选）：按「名+参数」或「连续失败签名」，减少误杀重试。
5. **进入 Phase 4**：Writer / Critic + 子 agent。

---

## 6. 30 秒速记卡

- **做了什么：** 给 `ToolRegistry.call` 装可插拔 before/after，落地权限/观测/错误恢复；loop 加卡死检测。  
- **Gate：** hook 测试绿；chat `/cost` 正确；工具失败能自纠错。  
- **一句话：** hook = 横切外挂；注册顺序即执行顺序；观测先于恢复；能吞 ≠ 该吞。  
- **面试答法：** 见 §4 五问；流程见 §1.5。  
- **踩坑：** Protocol 自递归；`error` 未初始化；吞异常条件写反；fixture 用 `BaseModel` 基类；zai 默认国际域名。  
- **下一站：** Phase 4（Phase 3 已完成）。
