from __future__ import annotations

from pathlib import Path

import pytest

from repo2resume.config import AppConfig, load_config, save_config


def test_save_and_load_roundtrip(data_dir: Path) -> None:
    cfg = AppConfig(
        llm_api_key="test-key",
        llm_model="zai/glm-5.2",
        llm_fallback_model="zai/glm-4.5-flash",
        name="Ada",
        email="ada@example.com",
        data_dir=data_dir,
    )
    save_config(cfg)

    loaded = load_config(data_dir)
    assert loaded.llm_api_key == "test-key"
    assert loaded.llm_model == "zai/glm-5.2"
    assert loaded.llm_fallback_model == "zai/glm-4.5-flash"
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
