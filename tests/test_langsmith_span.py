"""LangSmith 默认关闭；未装 SDK / 未设 key 时 span 为零开销 no-op。"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any

ls_mod = importlib.import_module("repo2resume.observability.langsmith_span")
from repo2resume.observability.langsmith_span import (  # noqa: E402
    finish_span,
    langsmith_span,
    span_call,
    tracing_enabled,
)


def test_tracing_disabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert tracing_enabled() is False


def test_tracing_requires_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert tracing_enabled() is False


def test_span_is_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    with langsmith_span("agent.turn", inputs={"user": "hi"}) as span:
        assert span is None
        finish_span(span, outputs={"ok": True})


def test_tracing_ignores_jwt_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "eyJhbGciOiJIUzI1NiJ9.not-lsv2")
    assert tracing_enabled() is False


def test_tracing_true_values(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    for flag in ("1", "true", "YES", "on"):
        monkeypatch.setenv("LANGSMITH_TRACING", flag)
        assert tracing_enabled() is True
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert tracing_enabled() is False


class _FakeRun:
    def __init__(self) -> None:
        self.ended: dict[str, Any] | None = None

    def end(self, **kwargs: Any) -> None:
        self.ended = kwargs


def test_span_call_records_outputs(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    runs: list[_FakeRun] = []

    @contextmanager
    def fake_trace(**kwargs: Any):
        _ = kwargs
        run = _FakeRun()
        runs.append(run)
        yield run

    monkeypatch.setattr(ls_mod, "_load_trace", lambda: fake_trace)
    out = span_call(
        "agent.turn",
        lambda: "hello-preview",
        inputs={"user": "hi"},
        outputs_of=lambda text: {"text": text},
    )
    assert out == "hello-preview"
    assert len(runs) == 1
    assert runs[0].ended is not None
    assert runs[0].ended["outputs"]["text"] == "hello-preview"


def test_span_call_records_error(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    runs: list[_FakeRun] = []

    @contextmanager
    def fake_trace(**kwargs: Any):
        _ = kwargs
        run = _FakeRun()
        runs.append(run)
        yield run

    monkeypatch.setattr(ls_mod, "_load_trace", lambda: fake_trace)

    def _boom() -> str:
        raise ValueError("nope")

    try:
        span_call("tool.x", _boom, run_type="tool")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert runs[0].ended is not None
    assert "ValueError" in runs[0].ended["error"]
