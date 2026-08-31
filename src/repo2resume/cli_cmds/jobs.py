"""repo2resume jobs"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from repo2resume.cli_cmds._common import console
from repo2resume.config import load_config
from repo2resume.llm.client import LLMClient
from repo2resume.storage.db import open_db
from repo2resume.storage.models import SkillProfile


def register(app: typer.Typer) -> None:
    @app.command()
    def jobs(
        query: Annotated[
            str | None,
            typer.Option(
                "--query", "-q", help="Job search query. Default: primary skill direction."
            ),
        ] = None,
        count: Annotated[
            int,
            typer.Option("--count", "-n", help="Number of jobs to show."),
        ] = 5,
        source: Annotated[
            str,
            typer.Option(
                "--source",
                help="Job source: auto|liepin_mcp|tavily|bocha|alibaba_top|mock.",
            ),
        ] = "auto",
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Write scored jobs as JSON."),
        ] = None,
    ) -> None:
        """Search and rank jobs from the latest skill profile."""
        from repo2resume.jobs.matcher import JobMatcher
        from repo2resume.jobs.search import search_jobs
        from repo2resume.retrieval.embedder import build_embedder

        cfg = load_config()
        db = open_db(cfg.db_path)
        try:
            profile = db.conn.execute(
                "SELECT payload_json FROM skill_profiles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if profile is None:
                console.print(
                    "[red]No skill profile found.[/red] Run `repo2resume analyze` first, "
                    "or use `repo2resume chat` to analyze your repos."
                )
                raise typer.Exit(code=1)

            skill_profile = SkillProfile.model_validate_json(profile["payload_json"])
            if query:
                queries = [query]
            else:
                queries = [
                    skill_profile.primary_direction,
                    *skill_profile.secondary_directions,
                ]
                queries = [q.strip() for q in queries if q and q.strip()]
            search_label = " / ".join(queries)

            embedder = build_embedder(cfg.embed_model, device="cpu")
            llm = LLMClient(cfg)
            matcher = JobMatcher(embedder, llm)

            seen: set[str] = set()
            found = []
            for q in queries:
                for j in search_jobs(q, count=max(count, 3), config=cfg, db=db, source=source):
                    if j.id not in seen:
                        seen.add(j.id)
                        found.append(j)
            if not found:
                console.print("[yellow]No jobs found.[/yellow]")
                raise typer.Exit(code=0)

            scores = matcher.match_all(
                skill_profile, found, top_k=max(count, min(len(found), count + 2))
            )
            scores = scores[:count]

            table = Table(title=f"Job matches for: {search_label}")
            table.add_column("Rank", justify="right")
            table.add_column("Title")
            table.add_column("Company")
            table.add_column("Overall", justify="right")
            table.add_column("Vector", justify="right")
            table.add_column("LLM", justify="right")
            table.add_column("Reason")
            for rank, s in enumerate(scores, start=1):
                job = next((j for j in found if j.id == s.job_id), None)
                title = job.title if job else s.job_id
                company = job.company if job else ""
                table.add_row(
                    str(rank),
                    title,
                    company or "—",
                    f"{s.overall_score:.0%}",
                    f"{s.vector_score:.0%}" if s.vector_score is not None else "—",
                    f"{s.llm_score:.0%}" if s.llm_score is not None else "—",
                    s.reason,
                )
            console.print(table)

            if output:
                payload = {
                    "query": search_label,
                    "jobs": [s.model_dump() for s in scores],
                }
                output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
                console.print(f"[green]Wrote[/green] {output}")
        finally:
            db.close()
