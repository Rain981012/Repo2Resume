"""工具/流水线进度：向下追加打印 + 可选心跳，避免 spinner 覆盖造成「卡住」错觉。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

ProgressCallback = Callable[[str], None]

_progress_cb: ContextVar[ProgressCallback | None] = ContextVar(
    "repo2resume_progress_cb",
    default=None,
)

# 心跳暂停：权限确认等需要用户输入时置位，避免刷屏盖住 y/N 提示
_heartbeat_paused = threading.Event()
# 进度回调可重入：emit_progress → _wrapped → outer_cb，必须用 RLock
_emit_lock = threading.RLock()
# 主线程 LLM 等待切片心跳（由 progress_heartbeat 安装）
_heartbeat_slice_s: ContextVar[float] = ContextVar(
    "repo2resume_heartbeat_slice_s",
    default=0.0,
)
_heartbeat_t0: ContextVar[float | None] = ContextVar(
    "repo2resume_heartbeat_t0",
    default=None,
)
# 当前阶段文案（不含「已等待」）；心跳 tick 复用它，避免盖成「处理中」
_progress_stage: ContextVar[str] = ContextVar(
    "repo2resume_progress_stage",
    default="处理中…",
)


def set_progress_callback(cb: ProgressCallback | None) -> Token:
    """注册当前上下文的进度回调；返回 token 供 reset。"""
    return _progress_cb.set(cb)


def reset_progress_callback(token: Token) -> None:
    _progress_cb.reset(token)


def emit_progress(message: str) -> None:
    """推送一条进度（有回调则调用；无回调静默）。暂停心跳期间仍允许手动 emit。

    顺带在安全点检查 CancelToken（长流水线各阶段之间可响应 Ctrl-C）。
    """
    from repo2resume.agent.cancel import check_cancelled

    check_cancelled()
    text = (message or "").strip()
    if not text:
        return
    cb = _progress_cb.get()
    if cb is not None:
        with _emit_lock:
            cb(text)


def pause_heartbeat() -> None:
    """暂停自动心跳（不阻止手动 emit_progress）。"""
    _heartbeat_paused.set()


def resume_heartbeat() -> None:
    _heartbeat_paused.clear()


@contextmanager
def heartbeat_paused() -> Iterator[None]:
    pause_heartbeat()
    try:
        yield
    finally:
        resume_heartbeat()


@contextmanager
def progress_heartbeat(interval_s: float = 10.0) -> Iterator[None]:
    """标记本轮 run 需要心跳提示。

    立刻打一行「处理中…」。后续「已等待 Ns」由主线程在 LLM 硬等待循环里
    每 interval_s 秒 emit（见 llm/client._completion_with_retry），避免后台线程
    写 stdout 在部分终端（如 Cursor）不可见。

    interval_s 写入 ContextVar；LLM 硬等待按该间隔在主线程切片醒来并打印。
    """
    cb = _progress_cb.get()
    state = {"last": "处理中…"}
    _heartbeat_paused.clear()

    outer_cb = cb

    def _wrapped(msg: str) -> None:
        if "（已等待" not in msg:
            state["last"] = msg
            _progress_stage.set(msg)
        if outer_cb is not None:
            with _emit_lock:
                outer_cb(msg)

    token = _progress_cb.set(_wrapped)
    interval_tok = _heartbeat_slice_s.set(float(interval_s) if interval_s > 0 else 0.0)
    t0_tok = _heartbeat_t0.set(time.monotonic())
    stage_tok = _progress_stage.set("处理中…")

    if outer_cb is not None:
        with _emit_lock:
            outer_cb(state["last"])

    try:
        yield
    finally:
        _progress_cb.reset(token)
        _heartbeat_slice_s.reset(interval_tok)
        _heartbeat_t0.reset(t0_tok)
        _progress_stage.reset(stage_tok)
        _heartbeat_paused.clear()


def emit_wait_tick() -> None:
    """主线程 LLM 等待切片到期时调用；尊重 pause_heartbeat。"""
    if _heartbeat_paused.is_set():
        return
    if _heartbeat_slice_s.get() <= 0:
        return
    t0 = _heartbeat_t0.get()
    if t0 is None:
        return
    elapsed = int(time.monotonic() - t0)
    stage = (_progress_stage.get() or "处理中…").strip() or "处理中…"
    # 直接走回调，避免再改 stage
    text = f"{stage}（已等待 {elapsed}s）"
    cb = _progress_cb.get()
    if cb is None:
        return
    from repo2resume.agent.cancel import check_cancelled

    check_cancelled()
    with _emit_lock:
        cb(text)


def heartbeat_wait_slice() -> float:
    """LLM 硬等待切片秒数；0 表示本轮未启用 chat 心跳，可一次等满 timeout。"""
    return float(_heartbeat_slice_s.get() or 0.0)
