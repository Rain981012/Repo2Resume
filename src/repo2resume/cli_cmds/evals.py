"""repo2resume evals"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from repo2resume.cli_cmds._common import console
from repo2resume.config import load_config
from repo2resume.evals.runner import (
    ALL_SUITES,
    SUITE_RESUME_PROG,
    compare_runs,
    load_run,
    results_dir,
    run_all,
    save_run,
)


def register(evals_app: typer.Typer) -> None:
    @evals_app.command("run")
    def evals_run(
        suite: Annotated[
            str,
            typer.Option(
                "--suite",
                help="retrieval | resume_prog | resume_judge | resume_e2e | jobs_match | all",
            ),
        ] = "all",
        no_llm: Annotated[
            bool,
            typer.Option("--no-llm", help="Skip LLM-as-judge and resume_e2e (CI / no API key)."),
        ] = False,
        search_backend: Annotated[
            str,
            typer.Option(
                "--backend",
                help="retrieval suite: fixture (lexical, CI) | hybrid (real RRF path)",
            ),
        ] = "fixture",
        save_baseline: Annotated[
            bool,
            typer.Option("--save-baseline", help="Also write evals/results/baseline.json"),
        ] = False,
        update_baseline: Annotated[
            bool,
            typer.Option("--update-baseline", help="Alias of --save-baseline"),
        ] = False,
        baseline: Annotated[
            Path | None,
            typer.Option("--baseline", help="Compare against this previous run JSON"),
        ] = None,
        out: Annotated[
            Path | None,
            typer.Option(
                "--out",
                help="Write this run JSON (default: evals/results/run_<ts>.json)",
            ),
        ] = None,
    ) -> None:
        """Run evaluation suites and optionally compare to a baseline."""
        cfg = load_config()
        save_baseline = save_baseline or update_baseline
        if suite == "all":
            suites = list(ALL_SUITES)
        elif suite in ALL_SUITES:
            suites = [suite]
        else:
            console.print(f"[red]Unknown suite: {suite}[/red]")
            raise typer.Exit(code=2)

        if search_backend not in ("fixture", "hybrid"):
            console.print(f"[red]Unknown --backend: {search_backend}[/red]")
            raise typer.Exit(code=2)

        console.print(
            f"[dim]Running suites: {', '.join(suites)} "
            f"(llm_judge={not no_llm}, retrieval_backend={search_backend})[/dim]"
        )
        results = run_all(
            config=cfg,
            suites=suites,
            use_llm_judge=not no_llm,
            search_backend=search_backend,  # type: ignore[arg-type]
        )

        rdir = results_dir()
        out_path = out or (rdir / f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
        save_run(results, out_path)
        console.print(f"[green]Saved[/green] {out_path}")

        if save_baseline:
            base_path = rdir / "baseline.json"
            save_run(results, base_path)
            console.print(f"[green]Baseline updated[/green] {base_path}")

        table = Table(title="Eval suite metrics")
        table.add_column("Suite")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_column("Note")
        failed = False
        for r in results:
            if r.error:
                note = f"[yellow]{r.error}[/yellow]"
                table.add_row(r.name, "—", "—", note)
                if not str(r.error).startswith("skipped"):
                    failed = True
                continue
            if not r.metrics:
                table.add_row(r.name, "—", "—", "[dim]no metrics[/dim]")
                continue
            first = True
            for key, val in r.metrics.items():
                note = f"backend={r.backend}" if first and r.backend else ""
                first = False
                table.add_row(
                    r.name,
                    key,
                    f"{val:.4f}" if isinstance(val, float) else str(val),
                    note,
                )
            if r.name == SUITE_RESUME_PROG and r.metrics.get("pass_rate", 1.0) < 1.0:
                failed = True
        console.print(table)

        baseline_path = baseline
        if baseline_path is None and (rdir / "baseline.json").exists() and not save_baseline:
            baseline_path = rdir / "baseline.json"
        if baseline_path is not None and baseline_path.exists():
            prev = load_run(baseline_path)
            cmp = Table(title=f"Compare vs {baseline_path.name}")
            cmp.add_column("Metric")
            cmp.add_column("Baseline", justify="right")
            cmp.add_column("Current", justify="right")
            cmp.add_column("Δ", justify="right")
            for name, b, c, delta in compare_runs(results, prev):
                bs = "—" if b is None else f"{b:.4f}"
                cs = "—" if c is None else f"{c:.4f}"
                ds = "—" if delta is None else f"{delta:+.4f}"
                cmp.add_row(name, bs, cs, ds)
            console.print(cmp)

        if failed:
            raise typer.Exit(code=1)
