from __future__ import annotations

import json
from pathlib import Path

import repo2resume.observability.run_context as rc
from repo2resume.observability.repro import append_verbose_prompt, write_repro
from repo2resume.observability.run_context import bind_data_dir, enter_agent_turn, exit_agent_turn


def _reset() -> None:
    rc._turn_depth.set(0)
    rc._obs_run_id.set(None)
    rc._data_dir.set(None)


def test_write_repro_scrubs_secrets(tmp_path: Path) -> None:
    _reset()
    bind_data_dir(tmp_path)
    enter_agent_turn()
    path = write_repro(extra={"llm_api_key": "sk-secret", "ok": 1})
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["llm_api_key"] == "***"
    assert data["ok"] == 1
    assert "sk-secret" not in path.read_text(encoding="utf-8")
    assert data["obs_run_id"]
    exit_agent_turn()
    _reset()


def test_write_repro_skipped_without_run(tmp_path: Path) -> None:
    _reset()
    bind_data_dir(tmp_path)
    assert write_repro() is None


def test_verbose_prompt_off_by_default(tmp_path: Path, monkeypatch) -> None:
    _reset()
    monkeypatch.delenv("REPO2RESUME_VERBOSE_TRACE", raising=False)
    bind_data_dir(tmp_path)
    enter_agent_turn()
    append_verbose_prompt([{"role": "user", "content": "secret-jd"}], span="llm.complete")
    files = list((tmp_path / "runs").glob("*.prompts.jsonl"))
    assert files == []
    exit_agent_turn()
    _reset()


def test_verbose_prompt_writes_when_enabled(tmp_path: Path, monkeypatch) -> None:
    _reset()
    monkeypatch.setenv("REPO2RESUME_VERBOSE_TRACE", "true")
    bind_data_dir(tmp_path)
    enter_agent_turn()
    append_verbose_prompt(
        [{"role": "user", "content": "hello", "api_key": "x"}],
        span="llm.complete.writer",
        model="zai/glm-4.7",
    )
    files = list((tmp_path / "runs").glob("*.prompts.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert line["span"] == "llm.complete.writer"
    assert line["messages"][0]["api_key"] == "***"
    exit_agent_turn()
    _reset()
