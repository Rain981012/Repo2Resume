"""本地复现包：默认不含 prompt / API key。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from repo2resume.observability.langsmith_span import _clip
from repo2resume.observability.run_context import (
    current_llm_model,
    current_run_id,
    current_session_id,
    git_sha,
)

_SECRET_KEYS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def verbose_trace_enabled() -> bool:
    flag = os.environ.get("REPO2RESUME_VERBOSE_TRACE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _data_dir() -> Path | None:
    from repo2resume.observability.run_context import current_data_dir

    return current_data_dir()


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(s in key for s in _SECRET_KEYS):
                out[k] = "***"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value[:20]]
    return value


def write_repro(*, extra: dict[str, Any] | None = None) -> Path | None:
    data_dir = _data_dir()
    rid = current_run_id()
    if data_dir is None or not rid:
        return None
    payload: dict[str, Any] = {
        "obs_run_id": rid,
        "session_id": current_session_id(),
        "git_sha": git_sha(),
        "llm_model": current_llm_model(),
        "langsmith_project": os.environ.get("LANGSMITH_PROJECT") or "",
        "langsmith_tracing": os.environ.get("LANGSMITH_TRACING") or "",
    }
    if extra:
        payload.update(_scrub(extra))
    path = data_dir / "runs" / f"{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def append_verbose_prompt(
    messages: list[dict[str, Any]],
    *,
    span: str,
    model: str | None = None,
) -> None:
    if not verbose_trace_enabled():
        return
    data_dir = _data_dir()
    rid = current_run_id()
    if data_dir is None or not rid:
        return
    path = data_dir / "runs" / f"{rid}.prompts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "obs_run_id": rid,
        "span": span,
        "model": model,
        "messages": _clip(_scrub(messages)),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
