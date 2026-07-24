"""Tool abstraction + registry — harness 扩展点。

【手写】填空题 — 把 pass / \"\"\"填空\"\"\" 换成真实代码，不要让 AI 整块生成。

为什么先写 tools，再写 loop？
  loop 只做「决策 → 调工具 → 回填」；加能力 = 注册工具，不动循环。
  所以 Tool / Registry 是 loop 的依赖，必须先有。

每做完一空跑: pytest tests/test_agent_tools.py -q

================================================================================
整体心智模型（先读这段，再写代码）
================================================================================

loop 要让 LLM 调用 Python 函数，但 LLM 和你的函数之间隔着两层：
  1. LLM 只懂 JSON：它不能 import 你的函数，只能输出
     {"name": "add", "arguments": {"a": 1, "b": 2}}
  2. 你的函数是 Python：签名、类型、docstring 都是 Python 世界的。

Tool = 把这两层粘起来的「适配器」：一个对象同时持有
  - 给 LLM 看的说明书（name / description / JSON Schema）
  - 真正能跑的 Python 函数（handler）
没有它，loop 就得写一堆 if-else 分发，每加一个工具改一次 loop。

ToolRegistry = 一堆 Tool 的「名册 + 分发台」。loop 每轮做三件事都靠它：
  - 告诉 LLM 有哪些工具可用   → list_schemas()
  - LLM 说「我要调 add」       → get("add")
  - 找到后执行                 → call("add", {...})
loop 只跟 Registry 打交道；加工具就 register，不动 loop。

================================================================================
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Hook：工具调用的生命周期钩子（Phase 2.5 Step 1）
# ---------------------------------------------------------------------------
# 挂在 ToolRegistry.call 内部，包住真正的 tool.call。每个 hook 是一个对象，
# 有 before / after 两个方法，按注册顺序依次跑：
#   before(name, args) → 返回 dict 则替换参数，返回 None 则不动，raise 则中止
#   after(name, args, result, error) → 返回值替换 result（Step 2 再处理 error）
#
# ============ 为什么要 hook（先读这段，再填空 7 / 空 8）============
#
# 没有 hook 时 call 是一行：`return self.get(name).call(arguments)`。
# 想加「权限 / 观测 / 事实核对」三件事，只能全塞进 call 里 →
#   1) call 越来越胖，每加一个关注点改一次核心方法（违反 OCP）
#   2) 权限/观测/业务核对挤在一起，改一条容易碰另一条
#   3) 没法按需组合（测试时想只跑观测不跑权限？得加 if 开关）
#   4) call 认识了所有具体关注点，职责越界（本该只管「找工具+执行」）
#
# hook = 在固定时机（before/after）自动跑的可插拔回调。call 只提供两个挂载点，
# 谁需要谁挂，互不相识：
#   PermissionHook.before  → 写文件/联网前问用户，只读放行
#   TraceHook.before/after → 记 name/参数/耗时/token 到 SQLite 的 llm_traces
#   FactSheetHook.after    → 把 analyze_repo 返回的数字记进 fact sheet，反幻觉
#   ErrorRecoveryHook.after→ 工具炸了把 error 转成文本回填给 LLM 重试
#
# Phase 2.5 的四块（hooks / 权限 / observability / error recovery）全都能用 hook
# 表达 → 所以先写 hooks，它是后面三块的地基。
#
# 优点：
#   - call 不再膨胀，加关注点 = 加 hook 类（OCP）
#   - 关注点分离，单测各管各的（测 TraceHook 不用造权限环境）
#   - 可插拔可组合：CLI 只挂 Trace，chat 挂全套，测试一个不挂
#   - 单一 chokepoint：所有工具调用必经 call，hook 一定生效不漏
#   - 顺序明确：按注册顺序串行，before 链改参数、after 链改结果
#   - 跟主流框架同构：LangChain pre_run/post_run、Django middleware、pytest teardown
#
# 取舍（面试加分）：
#   - 间接性：看 call 不知道实际跑了啥，得翻注册了哪些 hook
#   - 顺序依赖：before 链前一个改参数，后一个看到改过的；顺序错出 bug
#   - 异常语义复杂：before 抛了要不要跑 after？after 吞了要不要继续抛？→ 先定契约再写
#   - 过度抽象风险：只有一种关注点时上 hook 是杀鸡用牛刀，MVP 两个挂载点够了


class Hook(Protocol):
    """工具调用的生命周期钩子契约。"""

    def before(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """调工具前：返回 dict 替换参数，None 不动，raise 中止整个调用。"""
        ...

    def after(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        error: BaseException | None,
    ) -> Any:
        """调工具后：可改写 result（返回新值）；Step 2 起可处理/吞掉 error。"""
        ...


# ---------------------------------------------------------------------------
# Tool：一个工具的「说明书 + 执行器」
# ---------------------------------------------------------------------------


class Tool(BaseModel):
    """一个可被 LLM function-calling 调用的工具。

    三个字段各司其职：
      name          → LLM 调工具时喊的名字（"add"）
      description   → 决定「要不要调、调哪个」的自然语言说明
      params_model  → Pydantic 模型，一身两用：
                        a) model_json_schema()  → 生成 JSON Schema 给 LLM 看
                        b) model_validate(args) → 校验 LLM 回传的参数
                      用 Pydantic 而非手写 dict：校验和文档同源，不会漂移。
      handler       → 真正干活的 Python 函数，校验通过后才调它。
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    params_model: type[BaseModel]
    handler: Callable[..., Any]
    # 工具自声明的风险等级（事实）：readonly / write / network。
    # 权限策略（PermissionHook）基于此决定要不要问用户，工具自己不决定「要不要确认」。
    risk: Literal["readonly", "write", "network"] = "readonly"

    def json_schema(self) -> dict[str, Any]:
        """空 1（已完成）：生成 OpenAI / LiteLLM 风格的 function schema。

        为什么需要它：loop 每轮调 LLM 前要把「有哪些工具、参数长什么样」
        喂给 API（tools= 参数）。这个方法把 name/description/params_model
        打包成 API 能吃的那一项。

        期望输出形状（名字用 self.name）:
        {
          "type": "function",
          "function": {
            "name": "<self.name>",
            "description": "<self.description>",
            "parameters": <由 params_model 生成的 JSON Schema>
          }
        }
        提示: Pydantic v2 → MyModel.model_json_schema()
              可从 schema 里 pop 掉 "title" 等噪音字段（可选）。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
            },
        }

    def call(self, arguments: dict[str, Any]) -> Any:
        """空 2（已完成）：用 arguments 调 handler，返回结果。

        为什么需要它：LLM 回传的是裸 dict，可能缺字段、类型错。
        先用 params_model 校验，挡住乱传参；通过才喂给 handler。
        这是 harness「用确定性代码兜住概率性模型」的第一次落地。

        步骤提示:
          1) params = self.params_model.model_validate(arguments)
          2) 把 params 交给 handler——常用 handler(**params.model_dump())
          3) ValidationError 时不要吞掉，让上层（loop）决定如何回传给 LLM
        期望: call({"a": 1, "b": 2}) → handler 实参 a=1, b=2 的返回值
        """
        params = self.params_model.model_validate(arguments)
        return self.handler(**params.model_dump())


# ---------------------------------------------------------------------------
# ToolRegistry：一堆 Tool 的「名册 + 分发台」
# ---------------------------------------------------------------------------


class ToolRegistry:
    """工具注册表：按名字存放 / 列出 schema / 分发调用。

    loop 只跟它打交道，不直接碰单个 Tool。四个方法对应 loop 的四个动作：
      register()      → 启动时把工具一个个塞进去
      list_schemas()  → 每轮调 LLM 前：tools=registry.list_schemas()
      get()           → LLM 返回 tool_call 后：找到对应 Tool 对象
      call(name,args) → 找到后执行；是 get().call() 的薄封装，
                        方便以后在中间插权限/hook（Phase 2.5）
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._hooks: list[Hook] = []

    def register(self, tool: Tool) -> None:
        """空 3（已完成）：把 tool 放进 self._tools。

        为什么需要它：加能力 = register，不动 loop。
        规则: 同名重复注册 → 覆盖或 raise，二选一写清楚（建议 raise ValueError）。
        这里选 raise：避免静默覆盖，让 bug 早暴露。
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """空 4：按名取工具；没有则 KeyError 或 ValueError。

        为什么需要它：LLM 返回 {"name":"add",...}，loop 要按这个名字
        找到对应的 Tool 对象才能执行。没有就抛错——模型瞎编工具名时
        要让它知道「没这个工具」，而不是静默成功。
        """
        """
        填空: return self._tools[name]
        （KeyError 天然就有；想统一异常可 raise ValueError）
        """
        if name not in self._tools:
            raise ValueError(f"Tool {name} not found")
        return self._tools[name]

    def list_schemas(self) -> list[dict[str, Any]]:
        """空 5：返回所有已注册工具的 json_schema() 列表。

        为什么需要它：每轮调 LLM 前要告诉它「现在有哪些工具可用」。
        这个列表直接喂给 litellm.completion(tools=...)。
        期望: 注册了 echo、add → 长度为 2 的 schema 列表。
        """
        """
        填空: return [t.json_schema() for t in self._tools.values()]
        """
        return [t.json_schema() for t in self._tools.values()]

    def add_hook(self, hook: Hook) -> None:
        """空 7（Phase 2.5 Step 1）：注册一个生命周期钩子，调用时按注册顺序跑。

        提示: 一行——把 hook 追加进 self._hooks。
        """
        self._hooks.append(hook)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """空 8→空 9（Phase 2.5 Step 2，重写）：在 tool.call 前后跑 hooks + 处理异常。

        Step 1 只管成功路径；Step 2 要让 after 能见到异常，并允许 after 吞掉异常
        （把异常转成给 LLM 的文本，不炸出去——这是 error recovery 的基础）。

        契约:
          - 工具抛异常 → result=None, error=异常
          - after 链照常跑，每个 after 收到 (name, args, result, error)
          - after 返回非 None 且 error 非空 → 吞掉异常：result=返回值, error 置 None
          - after 返回 None   且 error 非空 → 不吞：error 保留
          - 成功路径（error 一直 None）after 返回值直接当新 result（同 Step 1）
          - 所有 after 跑完，error 仍非空 → re-raise

        步骤:
          1) args = arguments；跑 before 链（同 Step 1，不在本步改）
          2) try: result = self.get(name).call(args); error = None
             except BaseException as e: result = None; error = e
          3) for h in self._hooks:
                 new = h.after(name, args, result, error)
                 if error is not None and new is not None:
                     error = None          # 吞掉
                 result = new
          4) if error is not None: raise error
          5) return result

        期望（见 test_agent_hooks.py）:
          - 成功路径同 Step 1（翻倍/+1000/多 hook 串行）继续绿
          - 工具抛异常 + 无 hook → 异常原样抛出
          - 工具抛异常 + after 返回 "recovered: ..." → call 返回该字符串，不抛
          - 工具抛异常 + after 返回 None → 异常仍抛出
        """
        args = arguments
        for h in self._hooks:
            new = h.before(name, args)
            if new is not None:
                args = new
        try:
            result = self.get(name).call(args)
            error = None
        except Exception as e:
            result = None
            error = e
        for h in self._hooks:
            new = h.after(name, args, result, error)
            if error is not None and new is not None:
                error = None
            result = new
        if error is not None:
            raise error
        return result


# ---------------------------------------------------------------------------
# 练习用示例工具（测试会用；你可先读懂，不必改）
# ---------------------------------------------------------------------------


class _AddParams(BaseModel):
    a: int
    b: int


def make_add_tool() -> Tool:
    """示例：注册一个加法工具，方便你对照测试。"""
    return Tool(
        name="add",
        description="Add two integers.",
        params_model=_AddParams,
        handler=lambda a, b: a + b,
    )
