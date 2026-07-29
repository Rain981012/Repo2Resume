"""Typer entrypoint: init / analyze / jobs / resume / chat / export / evals."""

from __future__ import annotations

import typer

from repo2resume import __version__
from repo2resume.cli_cmds import register_all
from repo2resume.cli_cmds._common import configure_logging, console

app = typer.Typer(
    name="repo2resume",
    help="Analyze local git repos → skill profile → jobs → tailored Markdown resume.",
    no_args_is_help=True,
)
evals_app = typer.Typer(help="Evaluation commands.")
app.add_typer(evals_app, name="evals")

register_all(app, evals_app)


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    configure_logging(verbose)


@app.command()
def version() -> None:
    """Print package version."""
    console.print(__version__)


if __name__ == "__main__":
    app()
