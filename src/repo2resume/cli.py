"""Typer entrypoint: init / analyze / jobs / resume / chat / export / evals."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repo2resume import __version__
from repo2resume.agent.builtins import make_analyze_tool
from repo2resume.agent.context import ContextManager
from repo2resume.agent.hooks import (
    ErrorRecoveryHook,
    PermissionDenied,
    PermissionHook,
    TraceHook,
)
from repo2resume.agent.llm_adapter import make_llm_adapter
from repo2resume.agent.loop import AgentLoop
from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import ToolRegistry
from repo2resume.analysis.git_miner import AuthorInfo, MineOptions, collect_authors
from repo2resume.analysis.pipeline import run_analyze
from repo2resume.config import (
    DEFAULT_LLM_FALLBACK_MODEL,
    DEFAULT_LLM_MODEL,
    AppConfig,
    load_config,
    save_config,
)
from repo2resume.llm.client import LLMClient
from repo2resume.storage.cache import open_cache
from repo2resume.storage.db import open_db
from repo2resume.storage.models import SkillProfile

app = typer.Typer(
    name="repo2resume",
    help="Analyze local git repos → skill profile → jobs → tailored Markdown resume.",
    no_args_is_help=True,
)
evals_app = typer.Typer(help="Evaluation commands.")
app.add_typer(evals_app, name="evals")

console = Console()
logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    _configure_logging(verbose)


@app.command()
def version() -> None:
    """Print package version."""
    console.print(__version__)


def _provider_label(model: str) -> str:
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
    # If local_repos itself is a git repo, use it; else scan children.
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
    chosen = _parse_author_selection(raw, authors)
    console.print("[green]Selected:[/green] " + ", ".join(a.display for a in chosen))
    return [a.filter_value for a in chosen]


def _parse_author_selection(raw: str, authors: list[AuthorInfo]) -> list[AuthorInfo]:
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
    # preserve order, unique
    seen: set[int] = set()
    picked: list[AuthorInfo] = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            picked.append(authors[i - 1])
    return picked


@app.command("init")
def init_cmd(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip prompts; write defaults (API key from env if set).",
    ),
) -> None:
    """Interactive setup wizard → ~/.repo2resume/config.toml."""
    existing = load_config()
    data_dir = existing.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    if non_interactive:
        cfg = existing
        save_config(cfg)
        db = open_db(cfg.db_path)
        db.close()
        cache = open_cache(cfg.redis_url, cfg.cache_db_path)
        cache.close()
        console.print(f"[green]Config written to[/green] {cfg.config_path}")
        return

    console.print(Panel.fit("Repo2Resume setup", subtitle=str(data_dir)))

    model = typer.prompt(
        "Default LLM model",
        default=existing.llm_model or DEFAULT_LLM_MODEL,
    )
    fallback = typer.prompt(
        "Fallback LLM model (when quota exhausted; empty=none)",
        default=existing.llm_fallback_model or DEFAULT_LLM_FALLBACK_MODEL or "",
    )
    provider = _provider_label(model)
    api_key = typer.prompt(
        f"LLM API key ({provider})",
        default=existing.llm_api_key or "",
        hide_input=True,
        show_default=False,
    )
    name = typer.prompt("Your name", default=existing.name or "")
    email = typer.prompt("Email (git author filter)", default=existing.email or "")
    github = typer.prompt("GitHub URL or handle", default=existing.github or "")
    redis_url = typer.prompt("Redis URL", default=existing.redis_url)
    tavily = typer.prompt(
        "Tavily API key (optional, for job search)",
        default=existing.tavily_api_key or "",
        hide_input=True,
        show_default=False,
    )

    cfg = AppConfig(
        llm_api_key=api_key or None,
        llm_model=model,
        llm_fallback_model=fallback or None,
        writer_model=existing.writer_model,
        critic_model=existing.critic_model,
        embed_model=existing.embed_model,
        name=name or None,
        email=email or None,
        github=github or None,
        redis_url=redis_url,
        data_dir=data_dir,
        tavily_api_key=tavily or None,
        llm_timeout_s=existing.llm_timeout_s,
        llm_max_retries=existing.llm_max_retries,
    )
    save_config(cfg)

    db = open_db(cfg.db_path)
    db.close()
    cache = open_cache(cfg.redis_url, cfg.cache_db_path)
    backend = type(cache).__name__
    cache.close()

    console.print(f"[green]Saved[/green] {cfg.config_path}")
    console.print(f"[green]DB ready[/green] {cfg.db_path}")
    console.print(f"[green]Cache backend[/green] {backend}")
    if backend == "SqliteCache":
        console.print(
            "[yellow]Redis unreachable — using SQLite cache fallback "
            f"({cfg.cache_db_path}). Start Redis with: docker compose up -d[/yellow]"
        )


def _print_profile(profile: SkillProfile) -> None:
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

    _print_profile(profile)
    if output:
        output.write_text(profile.model_dump_json(indent=2))
        console.print(f"[green]Wrote profile[/green] {output}")


@app.command()
def jobs() -> None:
    """Search and rank jobs from the skill profile. (Phase 3)"""
    console.print("[yellow]Not implemented yet — coming in Phase 3.[/yellow]")
    raise typer.Exit(code=1)


@app.command()
def resume(
    job: Annotated[
        str | None,
        typer.Option("--job", help="Target job id."),
    ] = None,
) -> None:
    """Generate a resume draft for a job. (Phase 4)"""
    _ = job
    console.print("[yellow]Not implemented yet — coming in Phase 4.[/yellow]")
    raise typer.Exit(code=1)


@app.command()
def chat(
    resume: Annotated[
        str | None,
        typer.Option("--resume", "-r", help="恢复指定 session id 的历史对话。"),
    ] = None,
) -> None:
    """Interactive agent session. Analyze repos via natural language."""
    import uuid

    cfg = load_config()
    cache = open_cache(cfg.redis_url, cfg.cache_db_path)
    db = open_db(cfg.db_path)

    reg = ToolRegistry()
    reg.register(make_analyze_tool(cfg, cache, db))

    # Phase 2.5 Step 3：权限分级 hook。
    # 事实/策略分离：工具自声明 risk（Tool.risk），
    # 策略由 PermissionHook 按 needs_confirm_risks 决定。
    # 当前内置工具只有 analyze_repo（readonly），所以确认逻辑暂不会被触发；
    # 后续加 write/network 工具时，这里无需改动，工具自己标 risk 即可。
    def _confirm(name: str, args: dict) -> bool:
        preview = ", ".join(f"{k}={v}" for k, v in args.items())
        ans = console.input(
            f"[yellow]工具 {name}({preview}) 需要确认，执行吗？(y/N)[/yellow] "
        )
        return ans.strip().lower() in {"y", "yes"}

    reg.add_hook(
        PermissionHook(
            risk_of=lambda n: reg.get(n).risk,
            needs_confirm_risks={"write", "network"},
            confirm=_confirm,
        )
    )

    base = (
        "你是 Repo2Resume 的简历助手。可以调用 analyze_repo 工具分析本地 git 仓库，"
        "然后基于返回的统计摘要回答用户「我在某仓库做了什么」一类问题。"
        "analyze_repo 的 paths 为空时会自动扫描 ./local_repos/，所以用户没给路径时"
        "你不必追问，直接以空 paths 调用即可。不要编造数字；只引用工具返回的统计。回答用中文。"
    )
    assembler = PromptAssembler(base=base, registry=reg)
    ctx = ContextManager(system=assembler.build(None))

    # 会话持久化：--resume <id> 时从 SQLite 载入历史，否则开新 session
    if resume:
        history = db.load_session(resume)
        if history is None:
            console.print(f"[red]session {resume} not found[/red]")
            raise typer.Exit(code=1)
        ctx._messages = history
        session_id = resume
        console.print(f"[dim]resumed session {session_id} ({len(history)} msgs)[/dim]")
    else:
        session_id = uuid.uuid4().hex[:12]

    # Phase 2.5 Step 4：观测 hook。每次工具调用写一行 tool_traces，/cost 命令据此展示。
    # 注册顺序：PermissionHook 在前，其 before 拒绝时直接 raise，TraceHook.before 不会执行，
    # 故 TraceHook 的 _starts 栈不会残留（before/after 严格配对）。放在 session_id 确定之后。
    reg.add_hook(TraceHook(db, session_id=session_id))

    # Phase 2.5 Step 5：错误恢复 hook。工具异常被吞成文本回填 LLM，agent 可自纠错继续。
    # 必须挂在 TraceHook 之后：TraceHook.after 先跑（记下 error 到 trace），ErrorRecoveryHook.after
    # 后跑（吞掉 error 转文本）。顺序反了 TraceHook 会看到 error=None，失败就观测不到。
    # PermissionDenied 不吞（ErrorRecoveryHook 内部守卫），照常抛到这里被 catch 显示给用户。
    reg.add_hook(ErrorRecoveryHook())

    llm = make_llm_adapter(LLMClient(cfg, cache=cache))
    loop = AgentLoop(llm=llm, registry=reg, assembler=assembler, context=ctx, max_rounds=8)

    console.print(
        Panel(
            f"repo2resume chat — session {session_id}\n"
            "输入问题，Ctrl-D 或 /quit 退出。/cost 看工具调用统计。"
            "下次用 --resume {session_id} 续聊。",
            title="Chat",
            style="cyan",
        )
    )
    try:
        while True:
            try:
                user = console.input("[bold cyan]you > [/bold cyan]")
            except EOFError:
                break
            if not user.strip():
                continue
            if user.strip().lower() in {"/quit", "/exit"}:
                break
            if user.strip().lower() == "/cost":
                stats = db.tool_trace_stats(session_id)
                if stats["count"] == 0:
                    console.print("[dim]本 session 暂无工具调用记录[/dim]")
                else:
                    tbl = Table(title=f"session {session_id} 工具调用统计", style="cyan")
                    tbl.add_column("tool")
                    tbl.add_column("count", justify="right")
                    tbl.add_column("latency(ms)", justify="right")
                    tbl.add_column("errors", justify="right")
                    for name, s in stats["per_tool"].items():
                        tbl.add_row(name, str(s["count"]), str(s["ms"]), str(s["errors"]))
                    tbl.add_row(
                        "[bold]total[/bold]",
                        f"[bold]{stats['count']}[/bold]",
                        f"[bold]{stats['total_ms']}[/bold]",
                        f"[bold]{stats['errors']}[/bold]",
                    )
                    console.print(tbl)
                continue
            try:
                answer = loop.run(user)
            except PermissionDenied as exc:
                console.print(f"[yellow]已拒绝:[/yellow] {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]error:[/red] {exc}")
                continue
            console.print(Panel(answer, title="assistant", style="green"))
            # 每轮后持久化历史（不含 system；system 由 assembler 每次重建）
            db.save_session(session_id, ctx._messages, title=user[:40])
    finally:
        console.print(f"[dim]session {session_id} saved[/dim]")
        cache.close()
        db.close()


@app.command("export")
def export_cmd() -> None:
    """Export the current resume as Markdown. (Phase 4)"""
    console.print("[yellow]Not implemented yet — coming in Phase 4.[/yellow]")
    raise typer.Exit(code=1)


@evals_app.command("run")
def evals_run() -> None:
    """Run the evaluation suite. (Phase 5)"""
    console.print("[yellow]Not implemented yet — coming in Phase 5.[/yellow]")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
