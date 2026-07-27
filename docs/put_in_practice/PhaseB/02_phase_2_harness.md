# Phase B / Phase 2 实践复盘：手写 Harness 核心（面试复习稿）

> 项目：Repo2Resume  
> 阶段：阶段 B · **Phase 2 Harness 核心**（agent loop / tools / context / prompt assembler + chat 接线 + 会话持久化）  
> 用途：求职面试复习「你手写的 agent loop 长什么样、踩了什么坑」  
> 对应文档：`docs/design_docs/MVP_PLAN.md` §3 Phase 2、`DESIGN.md` §1.2（harness 组件表）  
> 验收日：2026-07-21（`repo2resume chat` 真跑，agent 自主调 `analyze_repo`）

---

## 1. 一句话定位

Phase 2 把「agent = LLM + 循环 + 工具」从概念落成代码：手写 tool-use 循环 + 工具注册表 + 上下文管理 + 提示词组装，再把 Phase 1 的 `analyze` 包成工具接进去，最后加会话持久化。`repo2resume chat` 能用自然语言分析仓库，关掉重开能续聊。

面试可说：

> 我手写了 ReAct 风格的 tool-use 循环（原生 function calling，不是文本协议），用依赖注入做单测，把 Phase 1 的分析能力包成工具注册进去。真跑时 agent 自主调 `analyze_repo`，回答的数字与 git 统计一致、不幻觉。

---

## 1.5 Phase 2 的完整逻辑顺序

Phase 2 的四个【手写】文件不是平铺的，有**构建顺序**和**运行时调用顺序**两条线。理解这两条线，就理解了 harness 怎么搭起来、又怎么跑起来。

### A. 构建顺序（为什么是这个顺序写）

依赖关系决定写法：后面的依赖前面的，所以必须按序。

```
① tools.py          —— 没有依赖，最先写
      │  定义 Tool（说明书+执行器）和 ToolRegistry（名册+分发台）
      ▼
② context.py        —— 依赖「消息格式」，独立于 tools
      │  定义 ContextManager（存历史、估 token、compaction）
      ▼
③ prompt_assembler  —— 依赖 ① 的 ToolRegistry（要拉工具清单）
      │  定义 PromptAssembler（每轮拼 system = base + state + tools）
      ▼
④ loop.py           —— 依赖 ①②③ 全部，最后写
      │  定义 AgentLoop.run（把前三者串成 decide-act-observe 循环）
      ▼
⑤ builtins.py       —— 【AI 辅助】把 Phase 1 的 run_analyze 包成 Tool，register 进 ①
      ▼
⑥ llm_adapter + chat —— 【AI 辅助】翻译 litellm 响应 + 接 REPL + 持久化
```

**为什么 tools 先、loop 后？** loop 只做「决策 → 调工具 → 回填」，它要调的「工具」和「上下文」必须先存在。先写 loop 会发现无处下手——你不知道 Tool 长什么样、context 怎么取消息。所以**先写被依赖的，最后写依赖别人的**。

### B. 运行时调用顺序（一次 `loop.run(user_input)` 内部发生了什么）

```
loop.run("分析一下我的仓库")
  │
  ├─ 准备：ctx.add("user", 输入)；ctx._system = assembler.build(state)
  │
  └─ for 每一轮（最多 max_rounds）:
       │
       ① 取消息：messages = ctx.messages()        ← context.py
       │          （system + 历史，system 每轮由 assembler 重建）
       │
       ② 取工具：tools = registry.list_schemas()   ← tools.py
       │          （每个 Tool 的 JSON Schema，喂给 LLM 的 tools= 参数）
       │
       ③ 调 LLM：resp = llm(messages, tools)        ← llm_adapter.py
       │          （litellm 返回 → 翻译成 LLMResponse{content, tool_calls}）
       │
       ├─ 若 resp.tool_calls 非空（模型要调工具）:
       │    │
       │    ④a 存 assistant 的 tool_calls 进历史   ← context.py（add(tool_calls=...)）
       │    ④b 执行：registry.call(name, args)     ← tools.py（校验 → handler）
       │    ④c 回填：ctx.add_tool_result(id, 结果) ← context.py
       │    ④d continue → 回到循环顶（再调 LLM）
       │
       └─ 否则（模型给纯文本）:
            ⑤ 存 assistant 回复进历史              ← context.py
            ⑥ return content  ← 这就是最终答案

     循环跑完仍没回复 → 返回「达到最大轮数」
```

### C. 每一步在做什么（一句话版）

| 步 | 文件 | 一句话 |
|---|---|---|
| ① | `context.py` | 把「system + 会话历史」拼成发给 LLM 的 messages |
| ② | `tools.py` | 把注册表里所有工具导出成 JSON Schema 列表，告诉 LLM「你能调啥」 |
| ③ | `llm_adapter.py` | 调 litellm，把它的原生响应翻译成 loop 认识的 `LLMResponse` |
| ④a | `context.py` | 把「模型决定调工具」这件事存进历史（OpenAI 协议要求带 tool_calls） |
| ④b | `tools.py` | 按名找到 Tool，Pydantic 校验参数，调 handler，拿结果 |
| ④c | `context.py` | 把工具结果按 `role=tool + tool_call_id` 回填进历史，下一轮 LLM 才看得到 |
| ⑤ | `context.py` | 把最终回复存进历史（下次续聊能接上） |
| ⑥ | `loop.py` | 返回纯文本，循环结束 |

### D. 串起来的一句话

> **构建时** tools → context → prompt_assembler → loop（被依赖的先写）；**运行时** loop 每轮调 context 取消息、调 tools 取 schema/执行、调 llm_adapter 调模型，循环直到模型给纯文本或达 max_rounds。loop 是「指挥」，tools/context/assembler 是「乐手」，llm_adapter 是「翻译」。

---

## 1.6 组件一览（细节见 §5 五问）

| 组件 | 一句话职责 |
| ---- | ---------- |
| Tool | 带说明书的可执行函数（schema + handler） |
| ToolRegistry | 名册 + 分发：register / list_schemas / call |
| ContextManager | 会话历史、回填 tool 结果、token/compaction |
| PromptAssembler | 每轮重建 system = base + state + tools |
| AgentLoop | decide → act → observe 循环 |
| LLM Adapter | litellm 原生响应 → `LLMResponse` |

串起来：Tool 是能力、Registry 是目录、Context 是记忆、Assembler 是简报、Loop 是指挥、Adapter 是翻译。

---

## 2. 本阶段完成了什么（对照计划）

| MVP 任务 | 档 | 状态 | 交付物 |
|---|---|---|---|
| `agent/tools.py`：Tool 抽象 + 注册表 | 【手写】 | ✅ | `Tool` / `ToolRegistry`，Pydantic → JSON Schema |
| ★ `agent/loop.py`：tool-use 循环 | 【手写】 | ✅ | `AgentLoop.run`，三停止条件 |
| `agent/context.py`：历史 + token 预算 + compaction | 【手写】 | ✅ | `ContextManager`，可注入 `count_tokens` |
| Prompt Assembler | 【手写】 | ✅ | `PromptAssembler`，base+state+tools 动态拼 |
| 把 Phase 1 分析包装成工具 | 【AI 辅助】 | ✅ | `agent/builtins.py` `make_analyze_tool` |
| 会话持久化（SQLite） | 【AI 辅助】 | ✅ | `chat_sessions` 表 + `--resume <id>` |
| 单测：mock LLM 断言循环分支 | 【AI 辅助】 | ✅ | 4 个 loop 测试 + adapter/builtins/db |

**本步交付物**

| 路径 | 说明 |
|---|---|
| `src/repo2resume/agent/tools.py` | Tool / ToolRegistry（填空式手写） |
| `src/repo2resume/agent/context.py` | ContextManager + compaction |
| `src/repo2resume/agent/prompt_assembler.py` | 动态 system prompt |
| `src/repo2resume/agent/loop.py` | ★ 手写 tool-use 循环 |
| `src/repo2resume/agent/builtins.py` | `analyze_repo` 工具（默认扫 `./local_repos/`） |
| `src/repo2resume/agent/llm_adapter.py` | litellm 响应 → loop 的 `LLMResponse`/`ToolCall` |
| `src/repo2resume/llm/client.py` | 新增 `complete_with_tools` |
| `src/repo2resume/cli.py` | `chat` 命令 + `--resume` |
| `src/repo2resume/storage/db.py` | `save/load/list_session` |
| `tests/test_agent_*.py` / `test_db_sessions.py` | 34 passed |

---

## 3. Phase 2 验收记录（Gate）

**验收标准（MVP）**：`repo2resume chat` 中说「分析一下某仓库」，agent 自主调工具并给出正确回答；关掉重开能续聊。

| 检查项 | 结果 |
|---|---|
| 全部 agent + db 单测 | **34 passed** |
| `repo2resume chat` 真跑 | ✅ agent 自主调 `analyze_repo` |
| 回答数字与 stats 一致 | ✅ socialdistribution 53/393、13.5%、Python 99.5% |
| `low_author_share` caution | ✅ 正确识别并提示简历口径 |
| 默认扫 `./local_repos/` | ✅ 不给 paths 也能跑（4 仓全分析） |
| 会话持久化 | ✅ `--resume <id>` 续聊 |

**Gate 判断：通过。** agent 不幻觉——数字全部来自工具返回的 stats，模型只做重组与建议。

---

## 4. 做法 · 影响 · 亮点 · 踩坑

### 4.1 做了什么

- **手写四件套**：`tools / context / prompt_assembler / loop` 全部走「脚手架填空」模式，AI 给签名 + 失败测试，自己填实现。
- **AI 辅助接线**：`builtins.py`（闭包注入 config/cache/db）、`llm_adapter.py`（翻译 litellm 响应）、`chat` REPL、会话持久化。
- **真跑迭代**：跑通后修了三个真实 bug（见踩坑）。

### 4.2 对项目的影响

- 从「CLI 命令」升级成「会自主调工具的 agent」——`chat` 是第一个真正体现 agent 性质的入口。
- harness 四件套就位，Phase 2.5 的 hooks / 权限 / observability / error recovery 都有挂载点（`ToolRegistry.call`、`AgentLoop.run` 循环体）。
- Phase 1 的 `analyze` 零改动被复用——证明「工具化 = 加 register 不动 loop」的设计成立。

### 4.3 亮点（可上面试）

1. **手写 loop 而非用框架**：能逐行讲清三停止条件（纯文本回复 / max_rounds / 异常），对照 LangChain AgentExecutor 的内核。
2. **依赖注入做单测**：`count_tokens` 和 `llm` 都可注入，loop 测试用「剧本式」假 LLM，不联网不花钱。
3. **确定性兜概率**：工具结果先 Pydantic 校验再喂 handler；`_parse_since` 容错——harness 思想在每层落地。
4. **状态放 system 不放历史**：会话持久化只存 `ctx._messages`，system 由 assembler 每轮重建，避免裁剪失忆 + system 漂移。

### 4.4 踩坑

**坑 1：litellm `tools=` 路径隐性依赖 `fastapi` + `orjson`**  
- 现象：`litellm.completion(tools=...)` 抛 `No module named 'fastapi'`，再 `orjson`。  
- 根因：litellm 传 tools 时惰性 import `litellm.proxy.litellm_pre_call_utils` → 拉 fastapi/orjson。**不带 tools 的普通 `complete()` 不触发**，所以 Phase 1 没暴露。  
- 教训：第三方库的「可选依赖」常按代码路径隐性触发；上 function calling 前要跑通 tools 路径。已加进 `pyproject.toml`。  
- 面试升华：依赖图里的「条件依赖」是生产事故源——单测要覆盖到触发该路径的用例。

**坑 2：模型把可选字段填成字符串 `"null"`**  
- 现象：`since` 传 `"null"`（不是真 null），`_parse_since` 套 `%Y-%m-%d` 抛错，4 仓全 skip。  
- 修法：双层防御——handler 清洗 `"null"/"none"/""` → None；`_parse_since` 解析失败返回 None（根治）。  
- 教训：**别假设模型会按 schema 传「空值」**；可选字段在边界要做归一化。  
- 面试升华：概率模型的输出要在边界用确定性代码归一化，这是 harness 的本职。

**坑 3：改了源码，长跑的 REPL 不生效**  
- 现象：修了 `_parse_since` 后，用户终端里仍报旧错。  
- 根因：chat REPL 是改代码前启动的，Python 进程把旧代码加载进内存，文件改动不影响已运行进程。  
- 教训：开发期改源码后必须重启长跑进程；editable install（`pip install -e .`）让 `repo2resume` 命令立即反映改动。  
- 面试升华：解释型语言无热 reload（除非上 reload 工具）——CI/部署要确保跑的是最新代码。

**坑 4：`list_sessions` 同秒排序打平**  
- 现象：两个 session 同秒保存，`ORDER BY updated_at DESC` 顺序不确定，测试偶发失败。  
- 修法：加 `rowid DESC` 作 tiebreaker。  
- 教训：时间戳秒精度不够；排序要有确定性 tiebreaker，否则测试 flaky。

---

## 5. 本阶段知识点（面试向 · 五问模板）

每个技术点按同一套问法答（与 Phase 3 复盘同标准）：

1. **X 是什么？**
2. **为什么要在 Repo2Resume 里用它？**
3. **为什么不用……（更偷懒 / 更常见的替代）？**
4. **有什么优缺点？**
5. **为什么不用其他同类技术 / 竞品？**

---

### ① Tool（工具抽象）

**1. 是什么？**  
把普通 Python 函数包装成 LLM 可调用的能力：`name` / `description` / `params_model`（Pydantic）/ `handler`。`json_schema()` 生成 function calling 说明书；`call(args)` 先校验再执行。

**2. 为什么要在这个项目用它？**  
LLM 只会生成文本，不会执行代码。要把「分析仓库」等能力交给 agent，必须有「说明书 + 执行器」。Pydantic 让 schema 与校验同源，不漂移。

**3. 为什么不用「loop 里一堆 if name == ...」直接调函数？**  
每加能力就改 loop，违反开闭原则；参数校验散落各处；没法统一 list_schemas 喂给模型。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 校验在前、执行在后，兜住乱传参 | 多一层抽象，简单脚本显得重 |
| schema 自动生成，与校验同源 | LLM 仍可能传 `"null"` 等脏值，边界还要清洗 |

**5. 为什么不用其他技术？**  
- **纯 JSON Schema 手写 dict：** 易与运行时校验漂移。  
- **MCP / 外部工具协议（当时）：** 学习目标是 harness 内核，先本地 Tool 即可；协议可后接。  
- **LangChain Tool 基类：** 能用但绑框架；本项目要手写讲清。

---

### ② ToolRegistry

**1. 是什么？**  
工具名册 + 分发台：`register` / `get` / `list_schemas` / `call`。loop 只跟 Registry 打交道。

**2. 为什么要在这个项目用它？**  
加能力 = register，不动 loop。Phase 1 的 analyze、Phase 3 的 search_jobs 都是同一挂载点。单一来源列出 schema，避免工具清单散落。

**3. 为什么不用「全局 dict 或在 loop 里硬编码工具列表」？**  
难测、难扩展、权限/hook 没有单一 chokepoint（Phase 2.5 的 hook 正是挂在 `call` 上）。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 可扩展、可隔离 | 间接性：看 loop 不知有哪些具体工具 |
| 为 hooks 预留 chokepoint | 同名重复注册需约定（本项目 raise） |

**5. 为什么不用其他技术？**  
- **依赖注入容器（复杂 IoC）：** MVP 过重。  
- **插件系统扫目录：** 可后做；先显式 register 更清晰。

---

### ③ ContextManager（会话上下文）

**1. 是什么？**  
管理发给 LLM 的消息序列：`add` / `add_tool_result` / `messages`；可选 `token_count` + `maybe_compact`。

**2. 为什么要在这个项目用它？**  
LLM 无状态，必须每次重发历史才能多轮工具调用。tool 结果要按 `role=tool + tool_call_id` 回填，模型才能看到观察。

**3. 为什么不用「只拼当前 user 一句、不存历史」？**  
多轮 tool-use 无法进行；续聊、`--resume` 也没基础。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 对话可接续；工具结果可观察 | 历史膨胀 → 需 compaction |
| system 不进 `_messages`，由 assembler 重建 | MVP「按条裁」可能拆散 tool_call 配对 |

**5. 为什么不用其他技术？**  
- **把一切塞进一条超长 user message：** 难维护协议，难裁剪。  
- **向量记忆当唯一记忆：** 丢精确 tool_call 配对；可作补充不能替代。  
- **LangChain Memory 类：** 黑盒；本项目要自己控消息形状。

---

### ④ PromptAssembler

**1. 是什么？**  
每轮把 system 拼成 `base + [state] + [tools 说明]` 的函数：`build(state) -> str`。

**2. 为什么要在这个项目用它？**  
工具集与状态会变；system 若写死或塞进历史，会被裁掉或漂移。每轮重建 = 视图，不是常量。

**3. 为什么不用「写死一个超长 system 字符串」？**  
加工具要手改字符串；状态过期；和持久化「只存历史」冲突。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 与 registry 自动同步工具说明 | 每轮重建有微小开销 |
| 状态放 system，不被 compaction 误删 | base 写太长仍会干扰模型（Phase 3 踩过） |

**5. 为什么不用其他技术？**  
- **Jinja 大模板文件当唯一来源：** 可用，但工具列表仍应来自 registry，避免双源。  
- **把状态当 user 消息插入：** 污染对话、易被裁。

---

### ⑤ AgentLoop（手写 tool-use 循环）

**1. 是什么？**  
`run(user_input) -> str`：ReAct 风格 **decide → act → observe**，直到纯文本或 `max_rounds`（及 Phase 2.5 卡死检测）。

**2. 为什么要在这个项目用它？**  
这是 agent 与「一问一答聊天」的本质差别。手写能逐行讲清停止条件与回填协议，是本项目技术核心（【手写】★）。

**3. 为什么不用「单次 LLM 调用、禁止多轮工具」？**  
分析仓库等任务必须先调工具拿数再回答；单次调用只能幻觉数字。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 行为透明、可单测注入假 LLM | 要自己处理死循环、异常、权限 |
| 面试能画图讲清 | 没有图编排，复杂多智能体要 Phase 4 再演进 |

**5. 为什么不用其他技术？**  
- **LangChain AgentExecutor / LangGraph：** 交付快但内核黑盒；本项目目标是吃透 harness。LangGraph 更适合后面多智能体状态图。  
- **纯文本 ReAct（抠 Action:）：** 易解析失败；本项目走**原生 function calling**（`tools=`），结构化更稳。  
- **固定流水线脚本：** 不是 agent，扩展性差。

**停止条件（必背）：** ① 纯文本回复；② `max_rounds`；③ 连续同名工具卡死（2.5）；工具异常由 hook 转文本而非直接炸进程。

---

### ⑥ LLM Adapter

**1. 是什么？**  
`make_llm_adapter(client)` → `llm(messages, tools) -> LLMResponse`。把 litellm 的 tool_calls 对象 / JSON 字符串 arguments 译成 loop 的数据结构。

**2. 为什么要在这个项目用它？**  
隔离第三方 SDK 脏细节；loop 只依赖自己的 `LLMResponse`（依赖倒置）。换供应商只改 adapter。

**3. 为什么不用「loop 里直接调 litellm.completion」？**  
loop 与供应商耦合；单测难；arguments 解析失败会炸循环。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 可替换后端；边界容错 | 多一层翻译，要跟上 SDK 变化 |
| 单测可整段替换假 llm | streaming 路径要额外适配（后来 Phase 3 加了） |

**5. 为什么不用其他技术？**  
- **直接绑 OpenAI SDK：** 换智谱/其他要大改。LiteLLM 统一多厂商，adapter 再收一层到本项目类型。  
- **HTTP 自封装：** 重复造轮子，MVP 不值。

---

### ⑦ 会话持久化（只存历史不存 system）

**1. 是什么？**  
SQLite 保存 `ctx._messages`；`--resume` 载入。system 每次由 assembler 重建。

**2. 为什么要在这个项目用它？**  
关掉重开能续聊是 MVP 验收；system 是「函数视图」，存进去会漂移、也难随工具注册更新。

**3. 为什么不用「把 system + 历史整包序列化」？**  
工具列表一变，旧 system 过期；compaction/状态策略难演进。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 续聊；system 始终新鲜 | 不存 system 则跨版本行为可能变（通常是优点） |
| 实现简单 | 要处理同秒排序等边角（rowid tiebreaker） |

**5. 为什么不用其他技术？**  
- **只存 Redis：** 易丢，不适合「会话档案」。本项目 Redis 用于缓存，会话落 SQLite。  
- **外键聊天平台：** 超范围。

---

### ⑧ 依赖注入单测（假 LLM / 假 count_tokens）

**1. 是什么？**  
把 `llm`、`count_tokens` 做成可注入参数；测试用剧本式假 LLM，不联网。

**2. 为什么要在这个项目用它？**  
loop 分支（直接回复 / 单工具 / 多轮 / 超限）必须可回归；真打 API 贵且不稳。

**3. 为什么不用「全靠手工 chat 点点点验收」？**  
不可回归、易漏分支、CI 跑不了。

**4. 优缺点？**  

| 优点 | 缺点 |
| ---- | ---- |
| 快、稳、零费用 | 假 LLM 演不到真实模型傻调工具；需真跑补充 |

**5. 为什么不用其他技术？**  
- **录制回放 HTTP：** 可做，维护成本高。  
- **只 integration 测：** 慢且 flaky。两者应互补：单测锁分支，真跑锁端到端。

---

### 速记对照表

| 我用了 | 为什么不用常见替代 | 一句话 |
| ------ | ------------------ | ------ |
| 手写 Loop | LangChain Executor / 写死脚本 | 吃透 harness；可讲停止条件 |
| 原生 function calling | 文本 ReAct 抠 Action | 结构化稳 |
| Tool + Registry | loop 内 if-else | 开闭；hook 有挂点 |
| Context + 回填 | 无历史单次调用 | 多轮工具必需 |
| Assembler 重建 system | 写死 / 存库 system | 防漂移 |
| Adapter | loop 直调 litellm | 依赖倒置 |
| 只存历史 | 整包存 system | system 是视图 |
| 注入假 LLM | 只靠真 API | 可回归 |

## 6. 下一步

1. ~~Phase 2.5~~ → 已完成，见 `03_phase_2_5_lifecycle_hooks.md`。
2. ~~Phase 3~~ → 已完成，见 `04_phase_3_retrieval_jobs.md`。
3. **compaction 升级**（仍待做）：按完整轮次裁，避免拆散 tool_call ↔ tool 结果。
4. **进入 Phase 4**：Writer / Critic、溯源 bullet、子 agent。

---

## 7. 30 秒速记卡

- **做了什么：** 手写 harness 四件套 + chat 接线 + 会话持久化；真跑 agent 自主分析仓库。  
- **Gate：** 34 tests 绿；chat 真跑数字与 stats 一致、不幻觉；`--resume` 续聊。  
- **一句话：** agent = LLM + 循环 + 工具；循环我手写，工具靠注册，上下文靠 compaction，状态放 system。  
- **面试答法：** 见 §5 — 每个组件按「是什么 → 为什么用 → 为什么不用… → 优缺点 → 为什么不用竞品」。  
- **流程：** 见 §1.5。  
- **踩坑：** litellm tools 隐性依赖 fastapi/orjson；模型传 `"null"`；长跑 REPL 不热更；同秒排序 flaky。  
- **下一站：** Phase 4（2.5/3 已完成）。
