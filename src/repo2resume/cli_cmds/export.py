"""repo2resume export"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from repo2resume.cli_cmds._common import console
from repo2resume.config import load_config
from repo2resume.storage.db import open_db
from repo2resume.storage.models import SkillProfile


def register(app: typer.Typer) -> None:
    @app.command("export")
    def export_cmd(
        output: Annotated[
            Path,
            typer.Option("--output", "-o", help="输出 Markdown 路径。"),
        ] = Path("resume_export.md"),
        strip_src: Annotated[
            bool,
            typer.Option("--strip-src", help="去掉 <!-- src -->（投递版）。"),
        ] = False,
        projects_only: Annotated[
            bool,
            typer.Option("--projects-only", help="只导出项目经历块。"),
        ] = False,
    ) -> None:
        """导出数据库中最新一份简历草稿为 Markdown。"""
        from repo2resume.resume.render import (
            render_project_experience,
            render_resume,
            write_markdown,
        )

        cfg = load_config()
        db = open_db(cfg.db_path)
        try:
            loaded = db.load_latest_resume_draft()
            if loaded is None:
                console.print(
                    "[red]No resume draft.[/red] "
                    "先运行 `repo2resume resume` 或 chat 里 generate_resume。"
                )
                raise typer.Exit(code=1)
            draft_id, draft = loaded
            profile = None
            row = db.conn.execute(
                "SELECT payload_json FROM skill_profiles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                profile = SkillProfile.model_validate_json(row["payload_json"])
            if projects_only:
                md = render_project_experience(draft, strip_src=strip_src)
            else:
                md = render_resume(draft, profile=profile, config=cfg, strip_src=strip_src)
            path = write_markdown(output, md)
            console.print(f"[green]Exported[/green] draft_id={draft_id} → {path}")
        finally:
            db.close()
