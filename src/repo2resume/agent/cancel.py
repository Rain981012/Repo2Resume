"""协作式取消：CancelToken + 安全点 check。

用法:
  with cancel_scope() as token:
      install soft SIGINT → token.cancel()
      loop.run(...)  # 内部 / emit_progress 会 check()
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token


class RunCancelled(Exception):
    """用户请求中断当前一轮 agent run（应回到 you >，不退出进程）。"""


class CancelToken:
    """线程安全的取消标志；SIGINT / 手动 cancel 共用。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.sigint_count = 0

    def cancel(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()
        self.sigint_count = 0

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise RunCancelled("用户中断")


_cancel_token: ContextVar[CancelToken | None] = ContextVar(
    "repo2resume_cancel_token",
    default=None,
)


def get_cancel_token() -> CancelToken | None:
    return _cancel_token.get()


def set_cancel_token(token: CancelToken | None) -> Token:
    return _cancel_token.set(token)


def reset_cancel_token(token: Token) -> None:
    _cancel_token.reset(token)


def check_cancelled() -> None:
    """有活跃 CancelToken 且已取消则抛 RunCancelled；无 token 时静默。"""
    tok = _cancel_token.get()
    if tok is not None:
        tok.check()


@contextmanager
def cancel_scope() -> Iterator[CancelToken]:
    """为本段 run 安装新的 CancelToken（嵌套时外层在 exit 恢复）。"""
    token = CancelToken()
    ctx_tok = set_cancel_token(token)
    try:
        yield token
    finally:
        reset_cancel_token(ctx_tok)
