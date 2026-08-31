"""一次用户输入对应一条观测 run（obs_run_id），与 SearchRun.run_id 无关。"""

from __future__ import annotations

import subprocess
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

_obs_run_id: ContextVar[str | None] = ContextVar("obs_run_id", default=None)
_turn_depth: ContextVar[int] = ContextVar("obs_turn_depth", default=0)
_session_id: ContextVar[str | None] = ContextVar("obs_session_id", default=None)
_llm_model: ContextVar[str | None] = ContextVar("obs_llm_model", default=None)
_data_dir: ContextVar[Path | None] = ContextVar("obs_data_dir", default=None)
_git_sha_cache: str | None = None


def bind_session(session_id: str | None) -> None:
    _session_id.set(session_id)


def bind_llm_model(model: str | None) -> None:
    _llm_model.set(model)


def bind_data_dir(path: Path | None) -> None:
    _data_dir.set(path)


def current_session_id() -> str | None:
    return _session_id.get()


def current_llm_model() -> str | None:
    return _llm_model.get()


def current_data_dir() -> Path | None:
    return _data_dir.get()


def turn_depth() -> int:
    return _turn_depth.get()


def current_run_id() -> str | None:
    return _obs_run_id.get()


def enter_agent_turn() -> str:
    """主 loop 在 depth=0 时发新 obs_run_id；嵌套的子 AgentLoop 复用。"""
    depth = _turn_depth.get()
    if depth == 0:
        _obs_run_id.set(f"turn-{uuid4().hex[:12]}")
    _turn_depth.set(depth + 1)
    return current_run_id() or ""


def exit_agent_turn() -> None:
    depth = _turn_depth.get()
    _turn_depth.set(max(0, depth - 1))


def git_sha() -> str:
    global _git_sha_cache
    if _git_sha_cache is not None:
        return _git_sha_cache
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        _git_sha_cache = f"{sha}-dirty" if dirty else sha or "unknown"
    except Exception:  # noqa: BLE001
        _git_sha_cache = "unknown"
    return _git_sha_cache
