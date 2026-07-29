"""repo2resume resume"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel

from repo2resume.cli_cmds._common import console
from repo2resume.config import load_config
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import open_cache
from repo2resume.storage.db import open_db
from repo2resume.storage.models import SkillProfile


def register(app: typer.Typer) -> None:
    @app.command()
    def resume(
        job: Annotated[
            str | None,
            typer.Option("--job", help="Target job id（来自 jobs 缓存）。"),
        ] = None,
        jd: Annotated[
            str | None,
            typer.Option("--jd", help="直接粘贴 JD 文本（可与 --job 二选一）。"),
        ] = None,
        output: Annotated[
            Path,
            typer.Option("--output", "-o", help="输出 Markdown 路径。"),
        ] = Path("resume_draft.md"),
        projects_only: Annotated[
            bool,
            typer.Option("--projects-only", help="只导出项目经历块。"),
        ] = False,
        strip_src: Annotated[
            bool,
            typer.Option("--strip-src", help="去掉 <!-- src --> 溯源注释（投递版）。"),
        ] = False,
    ) -> None:
        """针对职位生成简历草稿（Writer-Critic + 渲染）。"""
        from repo2resume.resume.pipeline import run_resume_pipeline
        from repo2resume.retrieval.embedder import build_embedder

        if not job and not (jd or "").strip():
            console.print("[red]请提供 --job <id> 或 --jd <文本>[/red]")
            raise typer.Exit(code=1)

        cfg = load_config()
        cache = open_cache(cfg.redis_url, cfg.cache_db_path)
        db = open_db(cfg.db_path)
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM skill_profiles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                console.print(
                    "[red]No skill profile.[/red] Run `repo2resume analyze` or chat 先分析。"
                )
                raise typer.Exit(code=1)
            profile = SkillProfile.model_validate_json(row["payload_json"])
            embedder = build_embedder(cfg.embed_model)
            llm = LLMClient(cfg, cache=cache)
            with console.status("[bold green]generating resume…[/bold green]"):
                result = run_resume_pipeline(
                    config=cfg,
                    db=db,
                    embedder=embedder,
                    llm=llm,
                    profile=profile,
                    jd_text=jd or "",
                    job_id=job,
                    output=output,
                    full_resume=not projects_only,
                    strip_src=strip_src,
                )
            approved = result.reports[-1].approved if result.reports else False
            console.print(
                f"[green]Wrote[/green] {result.output_path}  "
                f"(draft_id={result.draft_id}, critic_rounds={len(result.reports)}, "
                f"approved={approved})"
            )
            console.print(Panel(result.markdown[:3000], title="Preview", style="cyan"))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        finally:
            cache.close()
            db.close()
