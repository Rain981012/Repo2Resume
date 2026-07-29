"""repo2resume evals"""

from __future__ import annotations

import typer

from repo2resume.cli_cmds._common import console


def register(evals_app: typer.Typer) -> None:
    @evals_app.command("run")
    def evals_run() -> None:
        """Run the evaluation suite. (Phase 5)"""
        console.print("[yellow]Not implemented yet — coming in Phase 5.[/yellow]")
        raise typer.Exit(code=1)
