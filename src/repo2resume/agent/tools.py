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
from typing import Any

from pydantic import BaseModel

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

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """空 6：get(name).call(arguments) 的薄封装。

        为什么需要它：loop 里只写 registry.call("add", {...}) 比写
        registry.get("add").call({...}) 更顺、更短；更重要的是把
        「按名找 + 执行」收口到一个地方，Phase 2.5 才能在这里插
        pre-hook（权限/参数校验）和 post-hook（结果裁剪/事实清单核对）。
        """
        """
        填空: return self.get(name).call(arguments)
        """
        return self.get(name).call(arguments)


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
