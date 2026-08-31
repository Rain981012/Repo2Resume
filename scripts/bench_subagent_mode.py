#!/usr/bin/env python3
"""跑一轮真实 repo_analyst，把 SubAgentRunner.last_trace 写入 evals/results/。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--return-after-tools",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="True=直传工具原文；False=再总结（默认 False）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "evals" / "results" / "subagent_mode.json",
    )
    args = parser.parse_args()

    from repo2resume.agent.builtins import make_analyze_tool
    from repo2resume.agent.llm_adapter import make_llm_adapter
    from repo2resume.agent.progress import emit_progress, set_progress_callback
    from repo2resume.agent.subagent import SubAgentRunner, make_repo_analyst_spec
    from repo2resume.config import load_config
    from repo2resume.llm.client import LLMClient
    from repo2resume.storage.cache import open_cache
    from repo2resume.storage.db import open_db

    set_progress_callback(lambda msg: print(f"[progress] {msg}", flush=True))

    cfg = load_config()
    cache = open_cache(cfg.redis_url, cfg.cache_db_path)
    db = open_db(cfg.db_path)
    # 子代理决策不走缓存，避免把旧的「只调工具」轨迹当成这次对照。
    llm = make_llm_adapter(LLMClient(cfg, cache=None), temperature=0.0)
    analyze_tool = make_analyze_tool(cfg, cache, db)
    runner = SubAgentRunner(
        make_repo_analyst_spec(
            analyze_tool,
            return_after_tools=args.return_after_tools,
        ),
        llm=llm,
    )

    emit_progress(
        f"repo_analyst return_after_tools={args.return_after_tools} "
        f"model={cfg.llm_model} timeout_s={cfg.llm_timeout_s}"
    )
    t0 = time.perf_counter()
    try:
        summary = runner.run("分析本地仓库并总结技能方向")
    finally:
        cache.close()
        db.close()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    trace = runner.last_trace
    payload = {
        "model": cfg.llm_model,
        "llm_timeout_s": cfg.llm_timeout_s,
        "return_after_tools": args.return_after_tools,
        "wall_ms": wall_ms,
        "trace": None if trace is None else {
            "name": trace.name,
            "return_after_tools": trace.return_after_tools,
            "max_repeated_tool": trace.max_repeated_tool,
            "llm_calls": trace.llm_calls,
            "tool_names": trace.tool_names,
            "elapsed_ms": trace.elapsed_ms,
            "outcome": trace.outcome,
            "result_chars": trace.result_chars,
        },
        "summary_head": summary[:800],
        "summary_tail": summary[-400:] if len(summary) > 800 else "",
        "has_completion_marker": "【repo_analyst已完成】" in summary,
        "looks_stuck": "卡死" in summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)
    print(json.dumps(payload["trace"], ensure_ascii=False), flush=True)
    print(f"wall_ms={wall_ms:.0f} marker={payload['has_completion_marker']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
