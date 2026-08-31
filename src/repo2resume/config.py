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
# Writer / Critic 默认 Air：glm-4.7 审稿经常顶满 240s；可在 toml 里改回 4.7。
DEFAULT_WRITER_MODEL = "zai/glm-4.5-air"
DEFAULT_CRITIC_MODEL = "zai/glm-4.5-air"
# 默认用 Qwen3-Embedding-0.6B（~1.2GB，原生支持中文 + 代码，32K 上下文），
# 在「fork 用户冷启动」和「中英 + 代码检索质量」之间取平衡。
# 追求最高精度可切 local:Qwen3-Embedding-4B（~8GB，需 16GB+ RAM）。
# 详见 README「Embedding 模型选型」。
DEFAULT_EMBED_MODEL = "local:Qwen/Qwen3-Embedding-0.6B"


class AppConfig(BaseModel):
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_fallback_model: str | None = DEFAULT_LLM_FALLBACK_MODEL
    # 可选：覆盖 litellm provider 默认端点。例如 zai/ 默认打国际 api.z.ai，
    # 国内不走代理时设成 https://open.bigmodel.cn/api/paas/v4 直连智谱国内端点。
    llm_api_base: str | None = None
    writer_model: str | None = DEFAULT_WRITER_MODEL
    critic_model: str | None = DEFAULT_CRITIC_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL

    name: str | None = None
    email: str | None = None
    # 你的已知 git 身份列表（邮箱或名字，子串匹配）。analyze_repo 无 authors 时默认用它，
    # 解决「跨仓库多个 git 身份」问题（如学校邮箱 + GitHub noreply + 个人邮箱）。
    author_identities: list[str] = Field(default_factory=list)
    github: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Field(default_factory=lambda: DEFAULT_DATA_DIR)
    tavily_api_key: str | None = None
    # 博查 Web Search（仅 source=bocha 时用；auto 不再走 Bocha）
    bocha_api_key: str | None = None
    # 阿里招聘 TOP API（可选；配置后纳入 auto 搜索池）
    alibaba_top_app_key: str | None = None
    alibaba_top_app_secret: str | None = None
    # 猎聘官方 MCP（https://www.liepin.com/mcp/server 生成；配置后 auto 优先走猎聘）
    liepin_mcp_token: str | None = None
    liepin_mcp_url: str | None = None
    # LangSmith：写在 config.toml 即可；load_config 会 setdefault 到 LANGSMITH_*（SDK 只认环境变量）
    langsmith_api_key: str | None = None
    langsmith_project: str | None = None
    langsmith_tracing: bool = True

    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    # False：repo_analyst 调完工具后再总结（多步）；True：直接回传工具原文。
    subagent_return_after_tools: bool = False
    # 简历检索精排：off | llm | cross_encoder。默认 off（CE 会把技能表顶成主项目）。
    resume_reranker: str = "off"

    def model_for(self, role: str = "default") -> str:
        """Resolve model by role; None overrides fall back to llm_model."""
        if role == "embed":
            return self.embed_model
        overrides = {
            "writer": self.writer_model,
            "critic": self.critic_model,
        }
        return overrides.get(role) or self.llm_model

    def complete_model(self, role: str = "default") -> str | None:
        """传给 LLMClient.complete(model=...)。None 表示走 llm_model 并允许 fallback。"""
        chosen = self.model_for(role)
        if role == "default" or chosen == self.llm_model:
            return None
        return chosen

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


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _looks_like_langsmith_key(value: str | None) -> bool:
    """LangSmith personal/service key 以 lsv2_ 开头；JWT 等会 403。"""
    return bool(value) and value.strip().startswith("lsv2_")


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
    rat = _as_bool(
        pick("subagent_return_after_tools", "SUBAGENT_RETURN_AFTER_TOOLS", False),
        False,
    )
    tracing_raw = pick("langsmith_tracing", "LANGSMITH_TRACING")
    if tracing_raw is None:
        tracing_raw = os.environ.get("LANGSMITH_TRACING")

    env_ls_key = os.environ.get("LANGSMITH_API_KEY")
    if not _looks_like_langsmith_key(env_ls_key):
        env_ls_key = None
    env_ls_project = os.environ.get("LANGSMITH_PROJECT") or None

    cfg = AppConfig(
        llm_api_key=pick("llm_api_key", "LLM_API_KEY"),
        llm_model=pick("llm_model", "LLM_MODEL", DEFAULT_LLM_MODEL),
        llm_fallback_model=fallback,
        llm_api_base=pick("llm_api_base", "LLM_API_BASE"),
        writer_model=pick("writer_model", "WRITER_MODEL", DEFAULT_WRITER_MODEL),
        critic_model=pick("critic_model", "CRITIC_MODEL", DEFAULT_CRITIC_MODEL),
        embed_model=pick("embed_model", "EMBED_MODEL", DEFAULT_EMBED_MODEL),
        name=pick("name", "NAME"),
        email=pick("email", "EMAIL"),
        author_identities=pick("author_identities", "AUTHOR_IDENTITIES", []),
        github=pick("github", "GITHUB"),
        redis_url=pick("redis_url", "REDIS_URL", "redis://localhost:6379/0"),
        data_dir=resolved_dir,
        tavily_api_key=pick("tavily_api_key", "TAVILY_API_KEY"),
        bocha_api_key=pick("bocha_api_key", "BOCHA_API_KEY"),
        alibaba_top_app_key=pick("alibaba_top_app_key", "ALIBABA_TOP_APP_KEY"),
        alibaba_top_app_secret=pick("alibaba_top_app_secret", "ALIBABA_TOP_APP_SECRET"),
        liepin_mcp_token=pick("liepin_mcp_token", "LIEPIN_MCP_TOKEN")
        or os.environ.get("LIEPIN_USER_TOKEN")
        or None,
        liepin_mcp_url=pick("liepin_mcp_url", "LIEPIN_MCP_URL"),
        langsmith_api_key=env_ls_key or pick("langsmith_api_key", "LANGSMITH_API_KEY") or None,
        langsmith_project=env_ls_project or pick("langsmith_project", "LANGSMITH_PROJECT") or None,
        langsmith_tracing=_as_bool(tracing_raw, True),
        llm_timeout_s=float(timeout),
        llm_max_retries=int(retries),
        subagent_return_after_tools=rat,
        resume_reranker=str(pick("resume_reranker", "RESUME_RERANKER", "off") or "off"),
    )
    apply_langsmith_env(cfg)
    return cfg


def apply_langsmith_env(cfg: AppConfig) -> None:
    """SDK 只认 LANGSMITH_*。toml 有合法 key 时写入；已有 lsv2_ 环境变量不覆盖。"""
    env_key = (os.environ.get("LANGSMITH_API_KEY") or "").strip()
    toml_key = (cfg.langsmith_api_key or "").strip()
    if _looks_like_langsmith_key(toml_key) and not _looks_like_langsmith_key(env_key):
        os.environ["LANGSMITH_API_KEY"] = toml_key
    elif toml_key:
        os.environ.setdefault("LANGSMITH_API_KEY", toml_key)
    project = cfg.langsmith_project
    if not project and _looks_like_langsmith_key(os.environ.get("LANGSMITH_API_KEY") or toml_key):
        project = "repo2resume-local"
    if project:
        os.environ.setdefault("LANGSMITH_PROJECT", project)
    if os.environ.get("LANGSMITH_TRACING", "").strip():
        return
    key_present = _looks_like_langsmith_key(
        os.environ.get("LANGSMITH_API_KEY") or cfg.langsmith_api_key
    )
    if cfg.langsmith_tracing and key_present:
        os.environ["LANGSMITH_TRACING"] = "true"


def save_config(cfg: AppConfig) -> None:
    """Persist non-secret-friendly fields to config.toml under data_dir."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "llm_model": cfg.llm_model,
        "embed_model": cfg.embed_model,
        "redis_url": cfg.redis_url,
        "llm_timeout_s": cfg.llm_timeout_s,
        "llm_max_retries": cfg.llm_max_retries,
        "subagent_return_after_tools": cfg.subagent_return_after_tools,
        "resume_reranker": cfg.resume_reranker,
    }
    if cfg.llm_fallback_model:
        payload["llm_fallback_model"] = cfg.llm_fallback_model
    if cfg.llm_api_key:
        payload["llm_api_key"] = cfg.llm_api_key
    if cfg.llm_api_base:
        payload["llm_api_base"] = cfg.llm_api_base
    if cfg.writer_model:
        payload["writer_model"] = cfg.writer_model
    if cfg.critic_model:
        payload["critic_model"] = cfg.critic_model
    if cfg.name:
        payload["name"] = cfg.name
    if cfg.email:
        payload["email"] = cfg.email
    if cfg.author_identities:
        payload["author_identities"] = cfg.author_identities
    if cfg.github:
        payload["github"] = cfg.github
    if cfg.tavily_api_key:
        payload["tavily_api_key"] = cfg.tavily_api_key
    if cfg.bocha_api_key:
        payload["bocha_api_key"] = cfg.bocha_api_key
    if cfg.alibaba_top_app_key:
        payload["alibaba_top_app_key"] = cfg.alibaba_top_app_key
    if cfg.alibaba_top_app_secret:
        payload["alibaba_top_app_secret"] = cfg.alibaba_top_app_secret
    if cfg.liepin_mcp_token:
        payload["liepin_mcp_token"] = cfg.liepin_mcp_token
    if cfg.liepin_mcp_url:
        payload["liepin_mcp_url"] = cfg.liepin_mcp_url
    if cfg.langsmith_api_key:
        payload["langsmith_api_key"] = cfg.langsmith_api_key
    if cfg.langsmith_project:
        payload["langsmith_project"] = cfg.langsmith_project
    if not cfg.langsmith_tracing:
        payload["langsmith_tracing"] = False

    with cfg.config_path.open("wb") as f:
        tomli_w.dump(payload, f)
