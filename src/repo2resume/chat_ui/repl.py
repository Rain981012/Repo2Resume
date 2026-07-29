"""Chat REPL：输入循环、/cost、软 Ctrl-C、跑 AgentLoop。"""

from __future__ import annotations

import signal
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from repo2resume.agent.cancel import RunCancelled, cancel_scope
from repo2resume.agent.hooks import PermissionDenied
from repo2resume.agent.progress import (
    progress_heartbeat,
    reset_progress_callback,
    set_progress_callback,
)
from repo2resume.chat_ui.cost import print_session_cost
from repo2resume.chat_ui.progress_bind import make_on_event, make_on_progress
from repo2resume.chat_ui.setup import build_chat_runtime
from repo2resume.config import AppConfig, load_config
from repo2resume.jobs.usage import SearchUsageTracker, set_search_usage_tracker
from repo2resume.storage.cache import open_cache
from repo2resume.storage.db import open_db


@contextmanager
def soft_sigint(token: Any) -> Iterator[None]:
    """第一次 Ctrl-C → token.cancel()；连续第二次 → 恢复默认并抛 KeyboardInterrupt。"""
    previous = signal.getsignal(signal.SIGINT)

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        token.sigint_count += 1
        if token.sigint_count >= 2:
            signal.signal(signal.SIGINT, previous)
            raise KeyboardInterrupt
        token.cancel()
        # 信号处理器里尽量少做事；提示留给 REPL 捕获 RunCancelled 后打印

    signal.signal(signal.SIGINT, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def run_chat_session(
    *,
    resume: str | None = None,
    console: Console | None = None,
    cfg: AppConfig | None = None,
) -> None:
    """Interactive agent session. Analyze repos via natural language."""
    console = console or Console()
    cfg = cfg or load_config()
    cache = open_cache(cfg.redis_url, cfg.cache_db_path)
    db = open_db(cfg.db_path)

    chat_ui: dict[str, Any] = {"status": None, "streaming": False}

    if resume:
        history = db.load_session(resume)
        if history is None:
            console.print(f"[red]session {resume} not found[/red]")
            cache.close()
            db.close()
            raise typer.Exit(code=1)
        session_id = resume
        console.print(f"[dim]resumed session {session_id} ({len(history)} msgs)[/dim]")
    else:
        history = None
        session_id = uuid.uuid4().hex[:12]

    runtime = build_chat_runtime(
        cfg=cfg,
        cache=cache,
        db=db,
        session_id=session_id,
        console=console,
        chat_ui=chat_ui,
    )
    if history is not None:
        runtime.context._messages = history

    console.print(
        Panel(
            f"repo2resume chat — session {session_id}\n"
            "输入问题，Ctrl-D 或 /quit 退出。/cost 看成本。\n"
            "运行中 Ctrl-C：中断本轮；空闲时连按两次 Ctrl-C 退出（或 /quit）。\n"
            f"下次用 --resume {session_id} 续聊。",
            title="Chat",
            style="cyan",
        )
    )

    search_usage = SearchUsageTracker()
    set_search_usage_tracker(search_usage)
    on_progress = make_on_progress(console, chat_ui)
    on_event = make_on_event(console, chat_ui)
    # 空闲 you > 下的连按 Ctrl-C：第一次提示，第二次退出（与运行中 soft_sigint 对称）
    idle_sigint = 0

    try:
        while True:
            try:
                user = console.input("[bold cyan]you > [/bold cyan]")
            except EOFError:
                break
            except KeyboardInterrupt:
                idle_sigint += 1
                if idle_sigint >= 2:
                    console.print("\n[dim]退出中…[/dim]")
                    break
                console.print("\n[dim]再按一次 Ctrl-C 或输入 /quit 退出。[/dim]")
                continue
            idle_sigint = 0
            if not user.strip():
                continue
            if user.strip().lower() in {"/quit", "/exit"}:
                break
            if user.strip().lower() == "/cost":
                print_session_cost(console, db, session_id, search_usage)
                continue

            console.print(f"[dim]model: {cfg.llm_model}[/dim]")
            console.print("[dim]提示：Ctrl-C 可中断本轮[/dim]")

            progress_token = set_progress_callback(on_progress)
            answer: str | None = None
            interrupted = False
            try:
                with cancel_scope() as cancel_token:
                    with soft_sigint(cancel_token):
                        # 不用 console.status Live spinner：后台心跳打印会与之死锁
                        chat_ui["status"] = None
                        chat_ui["streaming"] = False
                        with progress_heartbeat(10.0):
                            answer = runtime.loop.run(user, on_event=on_event)
            except RunCancelled:
                interrupted = True
                console.print(
                    "[yellow]已中断本轮。[/yellow] "
                    "可继续输入新要求；再按一次 Ctrl-C 将退出进程。"
                )
                # 仍保存历史，避免中断后 --resume 丢上下文
                db.save_session(session_id, runtime.context._messages, title=user[:40])
                # 本轮已软中断：下一次空闲 Ctrl-C 视为「再按一次」直接退出
                idle_sigint = 1
                continue
            except PermissionDenied as exc:
                console.print(f"[yellow]已拒绝:[/yellow] {exc}")
                continue
            except KeyboardInterrupt:
                console.print("\n[dim]退出中…[/dim]")
                break
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]error:[/red] {exc}")
                continue
            finally:
                reset_progress_callback(progress_token)

            if interrupted or answer is None:
                continue

            if chat_ui["streaming"]:
                console.print()
            else:
                console.print(Panel(answer, title="assistant", style="green"))
            db.save_session(session_id, runtime.context._messages, title=user[:40])
    finally:
        console.print(f"[dim]session {session_id} saved[/dim]")
        cache.close()
        db.close()
