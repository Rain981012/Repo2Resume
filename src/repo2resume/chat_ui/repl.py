"""Chat REPL：输入循环、/cost、软 Ctrl-C、跑 AgentLoop。"""

from __future__ import annotations

import signal
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

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
from repo2resume.storage.db import Database, open_db


@dataclass(frozen=True)
class ChatSessionRef:
    """REPL 要用哪一段对话：id + 已落盘消息（新会话 history 为 None）。"""

    session_id: str
    history: list[dict[str, Any]] | None
    kind: Literal["continued", "explicit", "new"]


def resolve_chat_session(
    db: Database,
    *,
    resume: str | None = None,
    new: bool = False,
) -> ChatSessionRef:
    """无 flag 时续最近一次会话；--resume 指定 id；--new 强制新开。"""
    if resume and new:
        raise ValueError("不能同时使用 --resume 和 --new")
    if resume:
        history = db.load_session(resume)
        if history is None:
            raise LookupError(resume)
        return ChatSessionRef(session_id=resume, history=history, kind="explicit")
    if not new:
        recent = db.list_sessions(limit=1)
        if recent:
            sid = str(recent[0]["id"])
            history = db.load_session(sid)
            if history is not None:
                return ChatSessionRef(session_id=sid, history=history, kind="continued")
    return ChatSessionRef(
        session_id=uuid.uuid4().hex[:12],
        history=None,
        kind="new",
    )


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
    new: bool = False,
    console: Console | None = None,
    cfg: AppConfig | None = None,
) -> None:
    """Interactive agent session. Analyze repos via natural language."""
    console = console or Console()
    cfg = cfg or load_config()
    cache = open_cache(cfg.redis_url, cfg.cache_db_path)
    db = open_db(cfg.db_path)

    chat_ui: dict[str, Any] = {"status": None, "streaming": False}

    try:
        ref = resolve_chat_session(db, resume=resume, new=new)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        cache.close()
        db.close()
        raise typer.Exit(code=1) from exc
    except LookupError as exc:
        console.print(f"[red]session {exc} not found[/red]")
        cache.close()
        db.close()
        raise typer.Exit(code=1) from exc

    session_id = ref.session_id
    history = ref.history
    if ref.kind == "continued":
        console.print(
            f"[dim]已自动续上上次对话 session {session_id} "
            f"（{len(history or [])} msgs；--new 开新会话）[/dim]"
        )
    elif ref.kind == "explicit":
        console.print(f"[dim]resumed session {session_id} ({len(history or [])} msgs)[/dim]")

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

    from repo2resume.observability.run_context import (
        bind_data_dir,
        bind_llm_model,
        bind_session,
        current_run_id,
    )

    bind_session(session_id)
    bind_llm_model(cfg.llm_model)
    bind_data_dir(cfg.data_dir)
    last_obs_run_id: str | None = None

    console.print(
        Panel(
            f"repo2resume chat — session {session_id}\n"
            "输入问题，Ctrl-D 或 /quit 退出。/cost 看本会话成本，/cost last 看上一轮。\n"
            "运行中 Ctrl-C：中断本轮；空闲时连按两次 Ctrl-C 退出（或 /quit）。\n"
            "下次直接 repo2resume chat 会自动续上本段；repo2resume chat --new 开新会话。",
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
            if user.strip().lower() in {"/cost last", "/costlast"}:
                if not last_obs_run_id:
                    console.print("[dim]还没有完整的一轮对话。[/dim]")
                else:
                    print_session_cost(
                        console, db, session_id, search_usage, run_id=last_obs_run_id
                    )
                continue

            console.print(
                f"[dim]model: {cfg.llm_model}（搜岗/对话）  "
                f"writer: {cfg.model_for('writer')}  "
                f"critic: {cfg.model_for('critic')}[/dim]"
            )
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
                            last_obs_run_id = current_run_id()
            except RunCancelled:
                interrupted = True
                console.print(
                    "[yellow]已中断本轮。[/yellow] 可继续输入新要求；再按一次 Ctrl-C 将退出进程。"
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
                console.print(Panel(Text(answer), title="assistant", style="green"))
            db.save_session(session_id, runtime.context._messages, title=user[:40])
    finally:
        console.print(f"[dim]session {session_id} saved[/dim]")
        cache.close()
        db.close()
