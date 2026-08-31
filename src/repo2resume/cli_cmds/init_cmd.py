"""repo2resume init"""

from __future__ import annotations

import typer
from rich.panel import Panel

from repo2resume.cli_cmds._common import console, provider_label
from repo2resume.config import (
    DEFAULT_LLM_FALLBACK_MODEL,
    DEFAULT_LLM_MODEL,
    AppConfig,
    load_config,
    save_config,
)
from repo2resume.storage.cache import open_cache
from repo2resume.storage.db import open_db


def register(app: typer.Typer) -> None:
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
        provider = provider_label(model)
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
            "Tavily API key (optional, job search fallback after Bocha)",
            default=existing.tavily_api_key or "",
            hide_input=True,
            show_default=False,
        )
        bocha = typer.prompt(
            "Bocha API key (optional, for live job search)",
            default=existing.bocha_api_key or "",
            hide_input=True,
            show_default=False,
        )
        liepin = typer.prompt(
            "Liepin MCP token (optional, from https://www.liepin.com/mcp/server)",
            default=existing.liepin_mcp_token or "",
            hide_input=True,
            show_default=False,
        )

        cfg = AppConfig(
            llm_api_key=api_key or None,
            llm_model=model,
            llm_fallback_model=fallback or None,
            llm_api_base=existing.llm_api_base,
            writer_model=existing.writer_model,
            critic_model=existing.critic_model,
            embed_model=existing.embed_model,
            name=name or None,
            email=email or None,
            author_identities=existing.author_identities,
            github=github or None,
            redis_url=redis_url,
            data_dir=data_dir,
            tavily_api_key=tavily or None,
            bocha_api_key=bocha or None,
            alibaba_top_app_key=existing.alibaba_top_app_key,
            alibaba_top_app_secret=existing.alibaba_top_app_secret,
            liepin_mcp_token=liepin or None,
            liepin_mcp_url=existing.liepin_mcp_url,
            langsmith_api_key=existing.langsmith_api_key,
            langsmith_project=existing.langsmith_project,
            langsmith_tracing=existing.langsmith_tracing,
            llm_timeout_s=existing.llm_timeout_s,
            llm_max_retries=existing.llm_max_retries,
            subagent_return_after_tools=existing.subagent_return_after_tools,
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
