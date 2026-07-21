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

## 1.6 核心概念详解（每个组件是什么、为什么需要）

### ① Tool —— 一个「带说明书的可执行函数」

**是什么**：`Tool` 是把「一个普通 Python 函数」包装成「LLM 能理解并调用的能力」的容器。一个 Tool 里装着四样东西：

- `name`：工具名（LLM 调用时用，如 `analyze_repo`）
- `description`：自然语言说明（告诉 LLM「这个工具能干啥、什么时候该用」）
- `params_model`：一个 Pydantic 类，描述参数的结构和类型
- `handler`：真正干活的 Python 函数

**为什么需要**：LLM 本身只会生成文本，不会执行代码。要让 agent「能查仓库、能搜职位」，必须给它一个「可调用清单」。Tool 就是这个清单里的一项——它把「函数」翻译成「LLM 看得懂的说明书 + 可被安全调用的执行器」。

**两个关键方法**：
- `json_schema()`：把 Pydantic 参数类转成 JSON Schema，喂给 LLM 的 `tools=` 参数。LLM 据此知道「调这个工具要传什么字段、什么类型」。
- `call(arguments)`：拿到 LLM 给的参数字典，先用 Pydantic 校验（类型对不对、缺没缺字段），通过才调 handler。**校验在前、执行在后**——这是防 LLM 乱传参的护栏。

**类比**：Tool 像餐厅菜单上的一道菜——名字、描述、配料表（schema）写在菜单上给客人（LLM）看；真正做菜的是后厨（handler）。客人点菜（tool_call），服务员（Tool.call）先核对配料齐不齐，再下单给后厨。

### ② ToolRegistry —— 工具的「名册 + 分发台」

**是什么**：一个集中管理所有 Tool 的字典-like 容器，提供 `register / get / list_schemas / call` 四个方法。

**为什么需要**：
- **可扩展**：加新工具只需 `registry.register(tool)`，不用改 loop 一行代码。Phase 1 的 `analyze` 能零改动接进来，靠的就是这个。
- **单一来源**：loop 每轮从 registry 拉 schema 喂 LLM，从 registry 查名调 handler——工具清单只在一个地方维护，不会散落各处。
- **隔离**：loop 不认识具体工具，只认识 registry 这层接口。换工具、加工具、删工具，loop 无感。

**类比**：Registry 像公司的「内部服务目录」——员工（loop）不需要记住每个服务在哪，只要查目录（list_schemas 给 LLM 看）或按名调（call 转发给 handler）。

### ③ ContextManager —— 会话历史的「账房先生」

**是什么**：管理发给 LLM 的消息序列（messages）。它干三件事：
- `add(role, content, tool_calls=...)`：往历史里加一条消息（user / assistant / 带 tool_calls 的 assistant）
- `add_tool_result(id, result)`：把工具执行结果按 `role=tool + tool_call_id` 回填进历史
- `messages()`：吐出「system + 全部历史」给 LLM
- `token_count()` + `maybe_compact()`：估当前 token 数，超预算就压缩

**为什么需要**：
- **LLM 是无状态的**：每次调用都是独立请求，它不记得上一句说了啥。必须把完整历史每次重发，对话才能接上。ContextManager 就是这个「历史的载体」。
- **token 有上限**：历史越攒越长，迟早超模型窗口。compaction 是兜底——MVP 阶段「按条裁」（丢最老的几条），生产要「按完整轮次裁」避免拆散 tool_call 和它的结果配对。
- **system 不进历史**：system prompt 是「每轮重建的视图」（见 assembler），不存进 `_messages`，避免裁剪时丢掉、也避免不同状态间漂移。

**类比**：ContextManager 像法庭书记员——每句话都记下来（add），证人答完要归档（add_tool_result），法官要案卷时整理好递上去（messages），案卷太厚就抽掉最老的（compact）。

### ④ PromptAssembler —— system prompt 的「动态拼装车间」

**是什么**：每轮循环把 system prompt 拼成 `base + [当前状态] + [可用工具]` 三段。`build(state)` 返回一段字符串。

**为什么需要**：
- **system prompt 不是常量**：可用工具会变（注册了新工具）、当前状态会变（分析过哪个仓库、token 用了多少）。如果写死，就得每次手改；如果放历史里，又会被 compaction 裁掉。所以做成「每轮按当前状态重新拼」的函数。
- **状态放 system 不放历史**：状态信息（如「已分析 socialdistribution，主语言 Python 99.5%」）放 system 里，每轮都看得到、不被裁剪、不污染历史。会话持久化只存历史不存 system，正是这个道理。
- **工具说明自动同步**：注册新工具后，assembler 自动从 registry 拉 schema 渲染进 system，LLM 立刻知道有新工具可用。

**类比**：Assembler 像每天早会的「 briefing 撰稿人」——公司使命（base）不变，但今天的状态（state）和手头能用的资源（tools）每天变，他每天重新写一份简报发给大家。

### ⑤ AgentLoop —— agent 的「心脏 / 指挥」

**是什么**：一个 `run(user_input) -> str` 方法，内部是 ReAct 风格的循环：**decide（LLM 决策）→ act（调工具）→ observe（回填结果）→ decide ...** 直到模型给纯文本或达 max_rounds。

**为什么需要**：这是「agent 性质」的来源。没有 loop，LLM 只能一问一答；有了 loop，LLM 能「先调工具拿数据，再基于数据回答」——这就是 agent 和聊天机器人的本质区别。loop 把 tools / context / assembler / llm 四件套串起来，让「自主多步推理」成为可能。

**三个停止条件**（面试必背）：
1. LLM 返回纯文本（无 tool_calls）→ 任务完成，返回该文本
2. 达到 `max_rounds` → 防死循环的硬上限
3. （Phase 2.5 待加）工具异常 / 重复调用检测

**为什么手写不用框架**：手写能逐行讲清每一步在干啥；用 LangChain 的话内核是黑盒。面试问「你的 loop 长什么样」能画出 decide-act-observe 图，比说「我用了 LangChain」得分高。

**类比**：loop 像项目里的「执行 PM」——拿到需求（user_input），决定要不要查资料（调工具），查完再决定要不要再查，直到能给出结论（纯文本）或时间用完（max_rounds）。

### ⑥ LLM Adapter —— 「翻译官」

**是什么**：一个 `make_llm_adapter(client)` 工厂，返回 `llm(messages, tools) -> LLMResponse` 函数。把 litellm 的原生响应（带 `tool_calls` 对象、arguments 是 JSON 字符串）翻译成 loop 认识的 `LLMResponse{content, tool_calls: [ToolCall(id, name, arguments)]}`。

**为什么需要**：
- **隔离第三方 SDK 的脏细节**：litellm 的 tool_calls 是对象，arguments 是字符串要 `json.loads`，字段名跟 OpenAI 略有出入。loop 不该关心这些——它只想要「模型说了啥、要调啥工具、参数是啥」。adapter 把脏活全吞掉。
- **可替换 LLM 后端**：明天换 OpenAI SDK、换 Anthropic SDK，只改 adapter，loop 一行不动。这是「依赖倒置」——loop 依赖自己定义的 `LLMResponse` 接口，不依赖具体 SDK。
- **边界容错**：arguments 不是合法 JSON 时，adapter 吞掉异常返回空 dict，不让 loop 炸。又是「确定性兜概率」。

**类比**：adapter 像涉外会议的「同传」——外宾（litellm）说的话格式各异，同传统一翻成「中文简报」（LLMResponse），领导（loop）只听简报做决策。

### 串起来再看一遍

```
用户输入 ──→ loop.run()
              │ 每轮：
              ├─ context.messages()      取历史（账房递案卷）
              ├─ registry.list_schemas()  取工具清单（目录给 LLM 看）
              ├─ llm(messages, tools)     调模型（同传翻成 LLMResponse）
              │
              ├─ 有 tool_calls?
              │    ├─ context.add(tool_calls)   记下「模型要调工具」
              │    ├─ registry.call(name, args) 执行（校验+handler）
              │    └─ context.add_tool_result   回填结果
              │    └─ 继续下一轮
              │
              └─ 纯文本 → return（结束）
```

每个组件各司其职：**Tool 是能力、Registry 是目录、Context 是记忆、Assembler 是简报、Loop 是指挥、Adapter 是翻译**。少任何一块，agent 都跑不起来。

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

## 5. 本阶段知识点（面试向）

| 概念 | 本项目怎么练到 | 可背一句 |
|---|---|---|
| Agent = Model + Harness | 手写 loop + tools + context + assembler | harness 是模型外的一切：循环/工具/上下文/权限/恢复 |
| Tool-use 循环 | `AgentLoop.run` | decide → act → observe → decide，三停止条件 |
| Function calling 协议 | `Tool.json_schema` + `tool_call_id` 回填 | assistant 发 tool_call(带 id) → role=tool 回填对上 id |
| ReAct vs 原生 function calling | 本项目走原生 tools= | ReAct 文本抠 Action 易错；原生结构化更稳 |
| Context compaction | `maybe_compact` | 长会话不压缩就 context rot；MVP 按条裁，生产按轮次裁 |
| 依赖注入单测 | `count_tokens` / `llm` 可注入 | 外部依赖抽成参数，单测注入假实现，不联网 |
| Prompt as function | `PromptAssembler.build(state)` | system prompt 是每轮重建的视图，不是常量 |
| 会话持久化 | 只存历史不存 system | system 是函数；存了会漂移 |
| 确定性兜概率 | `_parse_since` 容错 + Pydantic 校验 | 边界用确定性代码归一化模型输出 |

**短答示例**

- **Q：你的 agent loop 和 LangGraph 一样吗？**  
  不一样。我的 loop 是普通 while 循环（decide-act-observe），没有图/节点/边；LangGraph 是状态图编排，适合多智能体。我这是 LangChain AgentExecutor 的手写内核，Phase 4 做 subagent 才会碰到图的味道。

- **Q：循环什么时候停？**  
  ① LLM 返回纯文本（无 tool_calls）→ 完成；② 达到 max_rounds → 防死循环；③（待加）工具异常 / 重复调用检测。max_rounds 是硬上限，必加。

- **Q：为什么工具结果要回填进历史？**  
  下一轮模型要看到「我上轮调了什么、拿到什么」才能继续推理。回填进历史 = 让工具结果成为会话一部分；代价是历史膨胀 → 需要 compaction。

- **Q：你的 loop 有什么坑？**  
  ① 模型反复调同一工具 → 死循环，靠 max_rounds 兜底（Phase 2.5 加重复检测）；② 工具异常目前直接炸，待加 error recovery；③ 没做权限/HITL。能说出「还没做」的边界比假装完美更得分。

---

## 6. 下一步

1. **Phase 2.5 Harness 加固**：lifecycle hooks（pre/post-tool）、权限分级（只读放行/写联网确认）、observability（trace 落 SQLite + `/cost`）、error recovery（异常回传 LLM 重试、重复调用卡死检测）。
2. **compaction 升级**：从「按条裁」改成「按完整轮次裁」，避免拆散 tool_call ↔ tool 结果配对。
3. **进入 Phase 3**：职位搜索 + 检索（Tavily / ChromaDB / BM25+向量混合）。

---

## 7. 30 秒速记卡

- **做了什么：** 手写 harness 四件套 + chat 接线 + 会话持久化；真跑 agent 自主分析仓库。  
- **Gate：** 34 tests 绿；chat 真跑数字与 stats 一致、不幻觉；`--resume` 续聊。  
- **一句话：** agent = LLM + 循环 + 工具；循环我手写，工具靠注册，上下文靠 compaction，状态放 system。  
- **踩坑：** litellm tools 隐性依赖 fastapi/orjson；模型传 `"null"` 字符串；长跑 REPL 不热更；同秒排序 flaky。  
- **下一站：** Phase 2.5 加固（hooks/权限/observability/恢复）→ Phase 3 检索。
