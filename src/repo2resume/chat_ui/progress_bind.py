"""Chat 进度 / tool_start 事件绑定。

进度输出必须线程安全：心跳在后台线程跑，Rich Console.print 在非主线程
经常不刷新/卡住，表现为「立刻有一行，超过 10s 却没有已等待」。
因此进度统一走 sys.stdout + flush。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console

from repo2resume.agent.progress import emit_progress

TOOL_STATUS = {
    "generate_resume": "生成简历…",
    "job_scout": "搜岗匹配中…",
    "repo_analyst": "分析仓库中…",
    "search_jobs": "搜索职位…",
    "analyze_repo": "分析仓库…",
    "find_project_materials": "检索项目素材…",
}


def _print_progress_line(message: str) -> None:
    """线程安全进度行（不经 Rich Live）。"""
    sys.stdout.write(f"… {message}\n")
    sys.stdout.flush()


def make_on_token(console: Console, chat_ui: dict[str, Any]) -> Callable[[str], None]:
    def _on_token(token: str) -> None:
        if not chat_ui.get("streaming"):
            chat_ui["streaming"] = True
        console.print(token, end="", highlight=False, soft_wrap=True)

    return _on_token


def make_on_progress(console: Console, chat_ui: dict[str, Any]) -> Callable[[str], None]:
    """可被心跳线程调用；不用 Rich Console.print。"""

    def _on_progress(message: str) -> None:
        _ = console, chat_ui
        _print_progress_line(message)

    return _on_progress


def make_on_event(console: Console, chat_ui: dict[str, Any]) -> Callable[[str, Any], None]:
    def _on_event(kind: str, data: Any) -> None:
        if kind == "tool_start":
            if chat_ui.get("streaming"):
                chat_ui["streaming"] = False
                sys.stdout.write("\n")
                sys.stdout.flush()
            label = TOOL_STATUS.get(str(data), f"calling {data}...")
            emit_progress(label)
        elif kind == "tool_done":
            pass
        elif kind == "llm_response":
            if data and data.tool_calls:
                names = ", ".join(tc.name for tc in data.tool_calls)
                if len(data.tool_calls) == 1:
                    label = TOOL_STATUS.get(names, f"calling {names}...")
                else:
                    label = f"calling {names}..."
                emit_progress(label)

    return _on_event
