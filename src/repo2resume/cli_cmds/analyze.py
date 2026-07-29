"""repo2resume analyze"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from repo2resume.analysis.git_miner import MineOptions
from repo2resume.analysis.pipeline import run_analyze
from repo2resume.cli_cmds._common import (
    console,
    discover_local_repos,
    print_profile,
    select_authors,
)
from repo2resume.config import load_config
from repo2resume.storage.cache import open_cache
from repo2resume.storage.db import open_db


def register(app: typer.Typer) -> None:
    @app.command()
    def analyze(
        paths: Annotated[
            list[str] | None,
            typer.Argument(help="Local git repo paths. Default: all repos under ./local_repos/."),
        ] = None,
        author: Annotated[
            list[str] | None,
            typer.Option(
                "--author",
                help="Skip interactive pick; filter by name/email substring (repeatable).",
            ),
        ] = None,
        since: Annotated[
            str | None,
            typer.Option("--since", help="Only commits after YYYY-MM-DD."),
        ] = None,
        stats_only: Annotated[
            bool,
            typer.Option("--stats-only", help="Skip LLM; print mined stats JSON."),
        ] = False,
        no_cache: Annotated[
            bool,
            typer.Option("--no-cache", help="Bypass analysis cache."),
        ] = False,
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Write profile/stats JSON to file."),
        ] = None,
    ) -> None:
        """Analyze local git repositories and build a skill profile.

        Paths are local filesystem directories (not GitHub URLs). With no paths given,
        scans ./local_repos/ for git repos. Authors are listed from git history for
        interactive selection unless --author is provided.
        """
        if paths:
            repo_paths = [Path(p).expanduser() for p in paths]
        else:
            repo_paths = discover_local_repos()
            if not repo_paths:
                console.print(
                    "[red]No repos found.[/red] Put git clones under "
                    f"[bold]{Path.cwd() / 'local_repos'}[/bold], or pass local paths:\n"
                    "  repo2resume analyze ~/code/my-repo"
                )
                raise typer.Exit(code=1)
            console.print(f"[dim]Using {len(repo_paths)} repo(s) from ./local_repos/[/dim]")

        if author:
            authors = list(author)
        else:
            authors = select_authors(repo_paths)

        options = MineOptions(authors=authors, since=since)
        cfg = load_config()
        cache = open_cache(cfg.redis_url, cfg.cache_db_path)
        db = open_db(cfg.db_path)
        try:
            stats, profile = run_analyze(
                repo_paths,
                options,
                config=cfg,
                cache=cache,
                db=db,
                stats_only=stats_only,
                use_cache=not no_cache,
            )
        finally:
            cache.close()
            db.close()

        if stats.errors:
            for err in stats.errors:
                console.print(f"[yellow]skip[/yellow] {err['path']}: {err['error']}")

        if stats_only or profile is None:
            payload = stats.model_dump(mode="json")
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            if output:
                output.write_text(text)
                console.print(f"[green]Wrote stats[/green] {output}")
            else:
                console.print_json(text)
            return

        print_profile(profile)
        if output:
            output.write_text(profile.model_dump_json(indent=2))
            console.print(f"[green]Wrote profile[/green] {output}")
