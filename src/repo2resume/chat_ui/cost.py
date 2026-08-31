"""Chat /cost 展示。"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from repo2resume.jobs.usage import SearchUsageTracker
from repo2resume.storage.db import Database


def print_session_cost(
    console: Console,
    db: Database,
    session_id: str,
    search_usage: SearchUsageTracker,
    *,
    run_id: str | None = None,
) -> None:
    tstats = db.tool_trace_stats(session_id, run_id=run_id)
    lstats = db.llm_trace_stats(session_id, run_id=run_id)
    sstats = search_usage.stats()
    scope = f"session {session_id}" + (f" run {run_id}" if run_id else "")
    if tstats["count"] == 0 and lstats["count"] == 0 and sstats["count"] == 0:
        if run_id:
            console.print(f"[dim]本轮 {run_id} 暂无调用记录[/dim]")
        else:
            console.print("[dim]本 session 暂无调用记录[/dim]")
        return

    if lstats["count"] > 0:
        ltbl = Table(title=f"{scope} LLM 调用成本", style="magenta")
        ltbl.add_column("model")
        ltbl.add_column("count", justify="right")
        ltbl.add_column("in tok", justify="right")
        ltbl.add_column("out tok", justify="right")
        ltbl.add_column("cost($)", justify="right")
        ltbl.add_column("latency(ms)", justify="right")
        for name, s in lstats["per_model"].items():
            ltbl.add_row(
                name,
                str(s["count"]),
                str(s["input_tokens"]),
                str(s["output_tokens"]),
                f"{s['cost_usd']:.6f}",
                str(s["latency_ms"]),
            )
        ltbl.add_row(
            "[bold]total[/bold]",
            f"[bold]{lstats['count']}[/bold]",
            f"[bold]{lstats['total_input']}[/bold]",
            f"[bold]{lstats['total_output']}[/bold]",
            f"[bold]{lstats['total_cost']:.6f}[/bold]",
            f"[bold]{lstats['total_latency_ms']}[/bold]",
        )
        console.print(ltbl)

    if sstats["count"] > 0:
        stbl = Table(
            title=f"{scope} 搜岗 API（Bocha/Tavily）",
            style="green",
        )
        stbl.add_column("provider")
        stbl.add_column("count", justify="right")
        stbl.add_column("ok", justify="right")
        stbl.add_column("errors", justify="right")
        stbl.add_column("results", justify="right")
        stbl.add_column("credits", justify="right")
        stbl.add_column("latency(ms)", justify="right")
        for name, s in sstats["per_provider"].items():
            stbl.add_row(
                name,
                str(s["count"]),
                str(s["ok"]),
                str(s["errors"]),
                str(s["results"]),
                f"{s['credits']:.1f}",
                str(s["latency_ms"]),
            )
        stbl.add_row(
            "[bold]total[/bold]",
            f"[bold]{sstats['count']}[/bold]",
            "",
            "",
            "",
            f"[bold]{sstats['total_credits']:.1f}[/bold]",
            f"[bold]{sstats['total_latency_ms']}[/bold]",
        )
        console.print(stbl)
        console.print(
            "[dim]credits：Tavily basic≈1/次；Bocha 按次记 1（美元单价以控制台为准）[/dim]"
        )

    if tstats["count"] > 0:
        tbl = Table(title=f"{scope} 工具调用统计", style="cyan")
        tbl.add_column("tool")
        tbl.add_column("count", justify="right")
        tbl.add_column("latency(ms)", justify="right")
        tbl.add_column("errors", justify="right")
        for name, s in tstats["per_tool"].items():
            tbl.add_row(name, str(s["count"]), str(s["ms"]), str(s["errors"]))
        tbl.add_row(
            "[bold]total[/bold]",
            f"[bold]{tstats['count']}[/bold]",
            f"[bold]{tstats['total_ms']}[/bold]",
            f"[bold]{tstats['errors']}[/bold]",
        )
        console.print(tbl)
