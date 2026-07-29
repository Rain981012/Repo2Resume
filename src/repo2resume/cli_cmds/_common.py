"""CLI 共享：Console、日志、作者选择、画像展示。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repo2resume.analysis.git_miner import AuthorInfo, collect_authors
from repo2resume.storage.models import SkillProfile

console = Console()
logger = logging.getLogger("repo2resume.cli")


def configure_logging(verbose: bool) -> None:
    """配置日志：默认只显示本项目 INFO；第三方库默认静音，避免刷屏。

    LiteLLM 会在 logger 上挂自己的 StreamHandler（level=DEBUG），只 setLevel 压不住，
    需要一并清空 handlers。--verbose 时放开所有 DEBUG（含第三方）。
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", force=True)
    if not verbose:
        for name in (
            "LiteLLM",
            "litellm",
            "LiteLLM Router",
            "LiteLLM Proxy",
            "openai",
            "httpx",
            "httpcore",
            "pydriller",
            "pydriller.repository",
        ):
            lg = logging.getLogger(name)
            lg.setLevel(logging.CRITICAL)
            lg.handlers.clear()
            lg.propagate = False
        try:
            import litellm

            litellm.set_verbose = False
            litellm.suppress_debug_info = True
            litellm.turn_off_message_logging = True
        except Exception:  # noqa: BLE001
            pass


def provider_label(model: str) -> str:
    """Human label for init prompts, derived from LiteLLM model prefix."""
    prefix = (model or "").split("/", 1)[0].lower()
    labels = {
        "zai": "Zhipu / Z.AI",
        "zhipu": "Zhipu",
        "gemini": "Gemini",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "openrouter": "OpenRouter",
        "deepseek": "DeepSeek",
    }
    return labels.get(prefix, prefix or "LLM")


def discover_local_repos(base: Path | None = None) -> list[Path]:
    """Find git repos under ./local_repos (or given base directory)."""
    root = base if base is not None else Path.cwd() / "local_repos"
    if not root.is_dir():
        return []
    if (root / ".git").exists() or (root / ".git").is_file():
        return [root.resolve()]
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and ((child / ".git").exists() or (child / ".git").is_file()):
            found.append(child.resolve())
    return found


def select_authors(repos: list[Path]) -> list[str]:
    """List authors from repos and let the user pick one or more."""
    authors = collect_authors(repos)
    if not authors:
        console.print("[red]No authors found in the selected repos.[/red]")
        raise typer.Exit(code=1)

    if len(authors) == 1:
        only = authors[0]
        console.print(f"[dim]Only one author found — using[/dim] {only.display}")
        return [only.filter_value]

    table = Table(title="Authors found in repos")
    table.add_column("#", justify="right")
    table.add_column("Author")
    table.add_column("Commits", justify="right")
    table.add_column("Repos")
    for i, a in enumerate(authors, start=1):
        table.add_row(
            str(i),
            a.display,
            str(a.commits),
            ", ".join(a.repos),
        )
    console.print(table)
    console.print("[dim]Enter numbers separated by comma (e.g. 1,3), or[/dim] [bold]all[/bold]")

    raw = typer.prompt("Select author(s)", default="1")
    chosen = parse_author_selection(raw, authors)
    console.print("[green]Selected:[/green] " + ", ".join(a.display for a in chosen))
    return [a.filter_value for a in chosen]


def parse_author_selection(raw: str, authors: list[AuthorInfo]) -> list[AuthorInfo]:
    text = raw.strip().lower()
    if text in {"all", "*"}:
        return list(authors)
    indices: list[int] = []
    for part in re.split(r"[,，\s]+", text):
        if not part:
            continue
        if not part.isdigit():
            console.print(f"[red]Invalid selection:[/red] {part!r}")
            raise typer.Exit(code=1)
        idx = int(part)
        if idx < 1 or idx > len(authors):
            console.print(f"[red]Out of range:[/red] {idx}")
            raise typer.Exit(code=1)
        indices.append(idx)
    if not indices:
        console.print("[red]No authors selected.[/red]")
        raise typer.Exit(code=1)
    seen: set[int] = set()
    picked: list[AuthorInfo] = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            picked.append(authors[i - 1])
    return picked


def print_profile(profile: SkillProfile) -> None:
    console.print(
        Panel.fit(
            f"[bold]{profile.primary_direction}[/bold]\n"
            + (", ".join(profile.secondary_directions) or "—"),
            title="Skill Profile",
        )
    )

    lang_table = Table(title="Coding languages")
    lang_table.add_column("Language")
    lang_table.add_column("Share", justify="right")
    lang_table.add_column("Evidence")
    for row in profile.coding_language:
        lang_table.add_row(row.name, f"{row.share:.1%}", row.evidence.render())
    console.print(lang_table)

    stack = profile.tech_stack
    stack_table = Table(title="Tech stack")
    stack_table.add_column("Category")
    stack_table.add_column("Items")
    stack_table.add_row("languages", ", ".join(stack.languages) or "—")
    stack_table.add_row("frameworks", ", ".join(stack.frameworks) or "—")
    stack_table.add_row("databases", ", ".join(stack.databases) or "—")
    stack_table.add_row("tools_and_infra", ", ".join(stack.tools_and_infra) or "—")
    stack_table.add_row("other", ", ".join(stack.other) or "—")
    console.print(stack_table)

    if profile.caution:
        console.print("[yellow]Caution[/yellow]")
        for c in profile.caution:
            console.print(f"  • {c}")
