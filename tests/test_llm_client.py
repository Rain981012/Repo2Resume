from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repo2resume.config import AppConfig
from repo2resume.llm.client import LLMClient, _is_quota_exhausted
from repo2resume.storage.cache import SqliteCache


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        _ = ttl
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def clear(self) -> None:
        self.store.clear()

    def close(self) -> None:
        pass


def test_complete_uses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_completion(**kwargs: Any) -> Any:
        _ = kwargs
        calls["n"] += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", fake_completion)
    monkeypatch.setattr(
        "repo2resume.llm.client.litellm.completion_cost",
        lambda **kwargs: 0.0,
    )

    cfg = AppConfig(llm_api_key="x", data_dir=tmp_path)
    cache = _FakeCache()
    client = LLMClient(cfg, cache=cache)
    messages = [{"role": "user", "content": "ping"}]

    r1 = client.complete(messages)
    r2 = client.complete(messages)
    assert r1.content == "hi"
    assert r2.content == "hi"
    assert calls["n"] == 1


def test_complete_without_cache_always_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def fake_completion(**kwargs: Any) -> Any:
        _ = kwargs
        calls["n"] += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="yo"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", fake_completion)
    monkeypatch.setattr(
        "repo2resume.llm.client.litellm.completion_cost",
        lambda **kwargs: None,
    )

    client = LLMClient(AppConfig(data_dir=tmp_path), cache=None)
    client.complete([{"role": "user", "content": "a"}])
    client.complete([{"role": "user", "content": "a"}])
    assert calls["n"] == 2


def test_sqlite_cache_integration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_completion(**kwargs: Any) -> Any:
        _ = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="cached"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", fake_completion)
    monkeypatch.setattr(
        "repo2resume.llm.client.litellm.completion_cost",
        lambda **kwargs: 0.01,
    )

    cache = SqliteCache(tmp_path / "cache.db")
    client = LLMClient(AppConfig(data_dir=tmp_path), cache=cache)
    assert client.complete([{"role": "user", "content": "z"}]).content == "cached"
    cache.close()


def test_quota_exhausted_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_completion(**kwargs: Any) -> Any:
        model = kwargs["model"]
        calls.append(model)
        if model == "zai/glm-5.2":
            err = RuntimeError("余额不足 / insufficient balance / quota exceeded")
            err.status_code = 403  # type: ignore[attr-defined]
            raise err
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="from-flash"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", fake_completion)
    monkeypatch.setattr(
        "repo2resume.llm.client.litellm.completion_cost",
        lambda **kwargs: 0.0,
    )

    cfg = AppConfig(
        data_dir=tmp_path,
        llm_model="zai/glm-5.2",
        llm_fallback_model="zai/glm-4.5-flash",
        llm_max_retries=1,
    )
    result = LLMClient(cfg).complete([{"role": "user", "content": "hi"}])
    assert result.content == "from-flash"
    assert result.model == "zai/glm-4.5-flash"
    assert calls[0] == "zai/glm-5.2"
    assert "zai/glm-4.5-flash" in calls


def test_is_quota_exhausted_detects_chinese_balance() -> None:
    assert _is_quota_exhausted(RuntimeError("账户余额不足"))
    assert not _is_quota_exhausted(RuntimeError("random network blip"))


def test_completion_hard_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """provider 忽略 timeout 时，Future 硬超时仍应打断。"""
    import time

    def hang(**kwargs: Any) -> Any:
        _ = kwargs
        time.sleep(5)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="late"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", hang)
    cfg = AppConfig(
        data_dir=tmp_path,
        llm_model="fake/model",
        llm_fallback_model=None,
        llm_timeout_s=0.2,
        llm_max_retries=1,
    )
    with pytest.raises(TimeoutError, match="超过"):
        LLMClient(cfg).complete([{"role": "user", "content": "hi"}], use_cache=False)


def test_completion_emits_wait_ticks_under_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chat 心跳开启时，主线程 LLM 等待应周期性 emit_wait_tick。"""
    import time

    from repo2resume.agent.progress import (
        progress_heartbeat,
        reset_progress_callback,
        set_progress_callback,
    )

    def hang(**kwargs: Any) -> Any:
        _ = kwargs
        time.sleep(0.35)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("repo2resume.llm.client.litellm.completion", hang)
    monkeypatch.setattr(
        "repo2resume.llm.client.litellm.completion_cost",
        lambda **kwargs: 0.0,
    )
    seen: list[str] = []
    token = set_progress_callback(seen.append)
    cfg = AppConfig(
        data_dir=tmp_path,
        llm_model="fake/model",
        llm_fallback_model=None,
        llm_timeout_s=2.0,
        llm_max_retries=1,
    )
    try:
        with progress_heartbeat(0.1):
            result = LLMClient(cfg).complete(
                [{"role": "user", "content": "hi"}], use_cache=False
            )
    finally:
        reset_progress_callback(token)
    assert result.content == "ok"
    assert any("已等待" in m for m in seen)
