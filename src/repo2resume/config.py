"""Load / save ~/.repo2resume/config.toml (+ env overrides)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field

ENV_PREFIX = "REPO2RESUME_"
DEFAULT_DATA_DIR = Path.home() / ".repo2resume"
# Zhipu: prefer GLM-5.2; free tier fallback is glm-4.5-flash.
DEFAULT_LLM_MODEL = "zai/glm-5.2"
DEFAULT_LLM_FALLBACK_MODEL = "zai/glm-4.5-flash"
DEFAULT_EMBED_MODEL = "openai/text-embedding-3-small"


class AppConfig(BaseModel):
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_fallback_model: str | None = DEFAULT_LLM_FALLBACK_MODEL
    writer_model: str | None = None
    critic_model: str | None = None
    embed_model: str = DEFAULT_EMBED_MODEL

    name: str | None = None
    email: str | None = None
    github: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Field(default_factory=lambda: DEFAULT_DATA_DIR)
    tavily_api_key: str | None = None

    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3

    def model_for(self, role: str = "default") -> str:
        """Resolve model by role; None overrides fall back to llm_model."""
        if role == "embed":
            return self.embed_model
        overrides = {
            "writer": self.writer_model,
            "critic": self.critic_model,
        }
        return overrides.get(role) or self.llm_model

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "repo2resume.db"

    @property
    def cache_db_path(self) -> Path:
        return self.data_dir / "cache.db"


def default_data_dir() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR


def _env(name: str) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(data_dir: Path | None = None) -> AppConfig:
    """Env vars override TOML; missing file → defaults."""
    root = data_dir or default_data_dir()
    file_data = _read_toml(root / "config.toml")

    def pick(key: str, env_name: str, default: Any = None) -> Any:
        env_val = _env(env_name)
        if env_val is not None and env_val != "":
            return env_val
        if key in file_data and file_data[key] is not None:
            return file_data[key]
        return default

    raw_dir = pick("data_dir", "DATA_DIR", str(root))
    resolved_dir = Path(str(raw_dir)).expanduser()

    timeout = pick("llm_timeout_s", "LLM_TIMEOUT_S", 60.0)
    retries = pick("llm_max_retries", "LLM_MAX_RETRIES", 3)
    fallback = pick("llm_fallback_model", "LLM_FALLBACK_MODEL", DEFAULT_LLM_FALLBACK_MODEL)
    if isinstance(fallback, str) and fallback.strip().lower() in {"", "none", "null"}:
        fallback = None

    return AppConfig(
        llm_api_key=pick("llm_api_key", "LLM_API_KEY"),
        llm_model=pick("llm_model", "LLM_MODEL", DEFAULT_LLM_MODEL),
        llm_fallback_model=fallback,
        writer_model=pick("writer_model", "WRITER_MODEL"),
        critic_model=pick("critic_model", "CRITIC_MODEL"),
        embed_model=pick("embed_model", "EMBED_MODEL", DEFAULT_EMBED_MODEL),
        name=pick("name", "NAME"),
        email=pick("email", "EMAIL"),
        github=pick("github", "GITHUB"),
        redis_url=pick("redis_url", "REDIS_URL", "redis://localhost:6379/0"),
        data_dir=resolved_dir,
        tavily_api_key=pick("tavily_api_key", "TAVILY_API_KEY"),
        llm_timeout_s=float(timeout),
        llm_max_retries=int(retries),
    )


def save_config(cfg: AppConfig) -> None:
    """Persist non-secret-friendly fields to config.toml under data_dir."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "llm_model": cfg.llm_model,
        "embed_model": cfg.embed_model,
        "redis_url": cfg.redis_url,
        "llm_timeout_s": cfg.llm_timeout_s,
        "llm_max_retries": cfg.llm_max_retries,
    }
    if cfg.llm_fallback_model:
        payload["llm_fallback_model"] = cfg.llm_fallback_model
    if cfg.llm_api_key:
        payload["llm_api_key"] = cfg.llm_api_key
    if cfg.writer_model:
        payload["writer_model"] = cfg.writer_model
    if cfg.critic_model:
        payload["critic_model"] = cfg.critic_model
    if cfg.name:
        payload["name"] = cfg.name
    if cfg.email:
        payload["email"] = cfg.email
    if cfg.github:
        payload["github"] = cfg.github
    if cfg.tavily_api_key:
        payload["tavily_api_key"] = cfg.tavily_api_key

    with cfg.config_path.open("wb") as f:
        tomli_w.dump(payload, f)
