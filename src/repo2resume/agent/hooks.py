"""Phase 2.5 lifecycle hooks 的具体实现：权限 / 观测 / 错误恢复。

挂在 ToolRegistry 上（registry.add_hook(...)），实现 agent/tools.py 的 Hook Protocol。
每个 hook 是一个独立关注点，职责正交，互不打架：
  - PermissionHook.before   → 只读放行 / 写联网要用户确认（拒绝则 raise）
  - TraceHook.before/after  → 记 trace 到 SQLite 的 llm_traces（Step 4）
  - ErrorRecoveryHook.after → 工具异常转文本回填给 LLM（Step 5）

为什么 hooks 单独一个文件：tools.py 是「框架」（Tool/Registry/Hook 契约），
hooks.py 是「实现」（具体关注点）。框架不认识具体 hook，加关注点只动这里。

事实/策略分离（Step 3 重构）：
  - Tool.risk 是「事实」——工具自声明自己是 readonly/write/network
  - PermissionHook 是「策略」——基于 risk 决定要不要问用户
  PermissionHook 不认识工具名，加新工具不用改权限；它通过注入的 risk_of(name)
  查工具的 risk，再按 needs_confirm_risks 集合决策。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class PermissionDenied(Exception):
    """PermissionHook.before 抛出 → 工具调用被拒绝，传到 loop 由它显示给 LLM/用户。"""


class PermissionHook:
    """权限分级 hook：基于工具的 risk 等级决定放行/确认/拒绝。

    before 语义:
      - risk = self._risk_of(name)                     # 查工具自声明的风险
      - risk 不在 needs_confirm_risks 集合 → 放行（return None）
      - risk 在 needs_confirm_risks 集合 → 调 confirm(name, arguments) 问用户:
          True  → 放行（return None）
          False → raise PermissionDenied（中止整个调用）
    after 语义:
      - 原样返回 result（权限不碰结果）

    依赖注入:
      - risk_of: Callable[[str], str] —— 生产传 lambda n: registry.get(n).risk，
        测试传 {"write_file":"write"}.get。PermissionHook 不持有 registry 引用，避免循环依赖。
      - confirm: Callable[[str, dict], bool] —— 生产传 input 封装，测试传 mock。
    """

    def __init__(
        self,
        risk_of: Callable[[str], str],
        needs_confirm_risks: set[str],
        confirm: Callable[[str, dict], bool],
    ) -> None:
        self._risk_of = risk_of
        self._needs_confirm_risks = needs_confirm_risks
        self._confirm = confirm

    def before(self, name: str, arguments: dict) -> dict | None:
        """空 10（Phase 2.5 Step 3，重构版）：按 risk 等级决策，拒绝则 raise PermissionDenied。

        步骤:
          1) risk = self._risk_of(name)
          2) if risk not in self._needs_confirm_risks: return None   # 只读放行
          3) ok = self._confirm(name, arguments)
          4) if not ok: raise PermissionDenied(f"用户拒绝执行 {name}")
          5) return None   # 放行
        """
        # TODO Step 3 重构: 按上面 5 步实现（用 risk_of + needs_confirm_risks）
        risk = self._risk_of(name)
        if risk not in self._needs_confirm_risks:
            return None
        ok = self._confirm(name, arguments)
        if not ok:
            raise PermissionDenied(f"用户拒绝执行 {name}")
        return None

    def after(self, name: str, arguments: dict, result: object, error: object) -> object:
        """权限不碰结果，原样返回。"""
        return result


class TraceHook:
    """观测 hook：每次工具调用写一行 trace 到 SQLite tool_traces 表。

    Phase 2.5 Step 4 — observability。不改控制流，只「看」。

    before/after 之间传起始时间的问题：
      Hook 协议没有显式通道把 before 的中间值传给 after，所以用一个栈
      self._starts: list[float]——before 压入 time.perf_counter()，after 弹出。
      单线程 AgentLoop 下严格配对（一次 before 必有一次 after），栈不会错位。

    依赖注入: db + session_id。session_id 由 cli 传入，使 /cost 能按 session 过滤。
    """

    def __init__(self, db: Any, session_id: str) -> None:
        self._db = db
        self._session_id = session_id
        self._starts: list[float] = []

    def before(self, name: str, arguments: dict) -> dict | None:
        """空 11（Step 4）：记起始时间，不改 args。

        步骤:
          1) self._starts.append(time.perf_counter())
          2) return None
        """
        self._starts.append(time.perf_counter())
        return None

    def after(self, name: str, arguments: dict, result: object, error: object) -> object:
        """空 12（Step 4）：算延迟、写库，原样返回 result。

        步骤:
          1) start = self._starts.pop()
          2) latency_ms = int((time.perf_counter() - start) * 1000)
          3) self._db.record_tool_trace(self._session_id, name, arguments,
                                         result if error is None else None, error, latency_ms)
          4) return result   # 观测不碰结果
        """
        # before 未执行就进 after（如未知工具名在更早 hook 失败）时不要 pop 空栈
        if not self._starts:
            return result
        start = self._starts.pop()
        latency_ms = int((time.perf_counter() - start) * 1000)
        self._db.record_tool_trace(
            self._session_id, name, arguments, result if error is None else None, error, latency_ms
        )
        return result

    def record_denied(self, name: str, arguments: dict, error: object) -> None:
        """PermissionHook 拒绝发生在 Trace.before 之前时，补记一行。"""
        latency_ms = 0
        if self._starts:
            start = self._starts.pop()
            latency_ms = int((time.perf_counter() - start) * 1000)
        self._db.record_tool_trace(self._session_id, name, arguments, None, error, latency_ms)


class ErrorRecoveryHook:
    """错误恢复 hook：工具抛异常时，把异常吞掉、转成文本回填给 LLM，让 agent 自纠错。

    Phase 2.5 Step 5。这是 Step 2 给 after 留的「吞异常」能力的真正用武之地：
      - 不吞：异常炸出 call → loop.run 抛出 → REPL 打印 error: ... → 对话中断
      - 吞掉：after 返回一段文本 → call 把它当 tool 结果 → LLM 看到「工具失败：原因」
              → LLM 可以换参数重试 / 换工具 / 告诉用户「这个我做不到」

    选择性吞（关键）:
      - PermissionDenied 不吞 —— 那是用户策略决定（拒绝执行），不是可重试的瞬时故障，
        必须抛出去给用户看。吞了就等于「用户拒绝后 agent 偷偷重试」，违反权限语义。
      - 其他异常都吞 —— 工具 bug、参数错、网络抖动等都转成文本交给 LLM 处理。

    注册顺序约束: 必须挂在 PermissionHook 之后。PermissionHook.before 拒绝时直接 raise，
    异常在 try 之前抛出，根本到不了 after，所以 ErrorRecovery 不会误吞 PermissionDenied。
    但仍加 isinstance 守卫做防御——万一未来有别的路径把 PermissionDenied 送进 after。
    """

    def before(self, name: str, arguments: dict) -> dict | None:
        """错误恢复不需要 before，原样放行。"""
        return None

    def after(self, name: str, arguments: dict, result: object, error: object) -> object:
        """空 13（Step 5）：选择性吞异常，转文本回填 LLM。

        三分支:
          1) error is None           → return result          # 成功路径不碰
          2) isinstance(error, PermissionDenied) → return None  # 不吞，让 call re-raise
          3) 其他 error              → return f"[工具 {name} 执行失败: "
                                        f"{type(error).__name__}: {error}] "
                                        f"请修正参数或换种方式后重试。"

        提示: 文本里带工具名 + 异常类型 + 异常消息，LLM 才能据此纠错。
        """
        if error is None:
            return result
        if isinstance(error, PermissionDenied):
            return None
        return (
            f"[工具 {name} 执行失败: {type(error).__name__}: {error}] 请修正参数或换种方式后重试。"
        )
