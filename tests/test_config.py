from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo2resume.config import AppConfig, load_config, save_config


def test_save_and_load_roundtrip(data_dir: Path) -> None:
    cfg = AppConfig(
        llm_api_key="test-key",
        llm_model="zai/glm-5.2",
        llm_fallback_model="zai/glm-4.5-flash",
        llm_api_base="https://open.bigmodel.cn/api/paas/v4",
        name="Ada",
        email="ada@example.com",
        data_dir=data_dir,
    )
    save_config(cfg)

    loaded = load_config(data_dir)
    assert loaded.llm_api_key == "test-key"
    assert loaded.llm_model == "zai/glm-5.2"
    assert loaded.llm_fallback_model == "zai/glm-4.5-flash"
    assert loaded.llm_api_base == "https://open.bigmodel.cn/api/paas/v4"
    assert loaded.name == "Ada"
    assert loaded.email == "ada@example.com"
    assert loaded.data_dir == data_dir


def test_env_overrides_toml(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(
        AppConfig(
            llm_model="zai/glm-5.2",
            name="FromFile",
            data_dir=data_dir,
        )
    )
    monkeypatch.setenv("REPO2RESUME_NAME", "FromEnv")
    monkeypatch.setenv("REPO2RESUME_LLM_MODEL", "zai/glm-4.5-flash")

    loaded = load_config(data_dir)
    assert loaded.name == "FromEnv"
    assert loaded.llm_model == "zai/glm-4.5-flash"


def test_model_for_fallback(data_dir: Path) -> None:
    cfg = AppConfig(data_dir=data_dir, writer_model=None, critic_model="zai/other")
    assert cfg.model_for("writer") == cfg.llm_model
    assert cfg.model_for("critic") == "zai/other"
    assert cfg.model_for("embed") == cfg.embed_model
    assert cfg.complete_model("writer") is None
    assert cfg.complete_model("critic") == "zai/other"


def test_writer_critic_models_roundtrip(data_dir: Path) -> None:
    cfg = AppConfig(
        data_dir=data_dir,
        llm_model="zai/glm-4.5-air",
        writer_model="zai/glm-4.7",
        critic_model="zai/glm-4.7",
    )
    save_config(cfg)
    loaded = load_config(data_dir)
    assert loaded.llm_model == "zai/glm-4.5-air"
    assert loaded.writer_model == "zai/glm-4.7"
    assert loaded.critic_model == "zai/glm-4.7"
    assert loaded.complete_model("writer") == "zai/glm-4.7"
    assert loaded.complete_model("critic") == "zai/glm-4.7"


def test_missing_critic_model_defaults_to_air(data_dir: Path) -> None:
    save_config(AppConfig(data_dir=data_dir, critic_model=None))
    loaded = load_config(data_dir)
    assert loaded.critic_model == "zai/glm-4.5-air"


def test_missing_writer_model_defaults_to_air(data_dir: Path) -> None:
    save_config(AppConfig(data_dir=data_dir, writer_model=None))
    loaded = load_config(data_dir)
    assert loaded.writer_model == "zai/glm-4.5-air"


def test_subagent_return_after_tools_from_toml_and_env(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(AppConfig(data_dir=data_dir, subagent_return_after_tools=True))
    loaded = load_config(data_dir)
    assert loaded.subagent_return_after_tools is True

    monkeypatch.setenv("REPO2RESUME_SUBAGENT_RETURN_AFTER_TOOLS", "false")
    overridden = load_config(data_dir)
    assert overridden.subagent_return_after_tools is False


def test_langsmith_toml_exports_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    save_config(
        AppConfig(
            data_dir=data_dir,
            langsmith_api_key="lsv2_from_toml",
            langsmith_project="proj-toml",
        )
    )
    loaded = load_config(data_dir)
    assert loaded.langsmith_api_key == "lsv2_from_toml"
    assert loaded.langsmith_project == "proj-toml"
    assert os.environ.get("LANGSMITH_API_KEY") == "lsv2_from_toml"
    assert os.environ.get("LANGSMITH_PROJECT") == "proj-toml"
    assert os.environ.get("LANGSMITH_TRACING") == "true"


def test_langsmith_env_not_overwritten_by_toml(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_already")
    monkeypatch.setenv("LANGSMITH_PROJECT", "proj-env")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    save_config(
        AppConfig(
            data_dir=data_dir,
            langsmith_api_key="lsv2_from_toml",
            langsmith_project="proj-toml",
        )
    )
    load_config(data_dir)
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_already"
    assert os.environ["LANGSMITH_PROJECT"] == "proj-env"


def test_resume_reranker_defaults_off(data_dir: Path) -> None:
    assert AppConfig(data_dir=data_dir).resume_reranker == "off"
    save_config(AppConfig(data_dir=data_dir))
    assert load_config(data_dir).resume_reranker == "off"


def test_resume_reranker_from_toml_and_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(AppConfig(data_dir=data_dir, resume_reranker="llm"))
    loaded = load_config(data_dir)
    assert loaded.resume_reranker == "llm"

    monkeypatch.setenv("REPO2RESUME_RESUME_RERANKER", "off")
    overridden = load_config(data_dir)
    assert overridden.resume_reranker == "off"


def test_langsmith_toml_replaces_non_lsv2_env(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "eyJhbGciOiJIUzI1NiJ9.not-a-langsmith-key")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    save_config(
        AppConfig(
            data_dir=data_dir,
            langsmith_api_key="lsv2_from_toml",
            langsmith_project="proj-toml",
        )
    )
    load_config(data_dir)
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_from_toml"
