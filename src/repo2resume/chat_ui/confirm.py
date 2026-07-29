"""Chat 权限确认：暂停心跳、截断预览、会话内记忆。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import Console

from repo2resume.agent.progress import emit_progress, heartbeat_paused


def confirm_key(name: str, args: dict) -> str:
    """忽略易变的 output_path，避免换文件名又弹一次。"""
    parts = [name]
    for key in sorted(k for k in args if k != "output_path"):
        text = str(args.get(key) or "").replace("\n", " ").strip()
        parts.append(f"{key}={text[:120]}")
    return "|".join(parts)


def make_confirm(
    console: Console,
    chat_ui: dict[str, Any],
    confirmed: set[str] | None = None,
) -> Callable[[str, dict], bool]:
    """返回 PermissionHook 用的 confirm(name, args) -> bool。"""
    remembered = confirmed if confirmed is not None else set()

    def _confirm(name: str, args: dict) -> bool:
        key = confirm_key(name, args)
        if key in remembered:
            emit_progress(f"沿用本会话已确认的 {name}…")
            return True

        with heartbeat_paused():
            console.print()
            console.print(
                f"[bold yellow]⚠ 工具 `{name}` 需要确认[/bold yellow]"
                f"（risk=写文件/联网）。请输入 [bold]y[/bold] 继续，其它键取消。"
            )
            for key_name, val in args.items():
                text = str(val).replace("\n", " ")
                if len(text) > 100:
                    text = text[:100] + "…"
                console.print(f"  [dim]{key_name}[/dim]: {text}")
            try:
                ans = console.input("[yellow]确认？(y/N) > [/yellow]")
            except EOFError:
                ans = ""
            ok = ans.strip().lower() in {"y", "yes"}
            if ok:
                remembered.add(key)
                emit_progress(f"已确认 {name}，继续执行…")
            else:
                console.print("[dim]已取消。[/dim]")
            return ok

    return _confirm
