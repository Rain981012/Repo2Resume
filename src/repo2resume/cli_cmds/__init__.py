"""CLI 子命令注册。"""

from __future__ import annotations

import typer


def register_all(app: typer.Typer, evals_app: typer.Typer) -> None:
    from repo2resume.cli_cmds import analyze, chat, evals, export, init_cmd, jobs, resume

    init_cmd.register(app)
    analyze.register(app)
    jobs.register(app)
    resume.register(app)
    chat.register(app)
    export.register(app)
    evals.register(evals_app)
