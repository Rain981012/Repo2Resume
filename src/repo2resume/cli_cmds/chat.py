"""repo2resume chat"""

from __future__ import annotations

from typing import Annotated

import typer

from repo2resume.chat_ui import run_chat_session
from repo2resume.cli_cmds._common import console
from repo2resume.config import load_config


def register(app: typer.Typer) -> None:
    @app.command()
    def chat(
        resume: Annotated[
            str | None,
            typer.Option("--resume", "-r", help="恢复指定 session id 的历史对话。"),
        ] = None,
    ) -> None:
        """Interactive agent session. Analyze repos via natural language."""
        run_chat_session(resume=resume, console=console, cfg=load_config())
