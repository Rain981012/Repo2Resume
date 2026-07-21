"""Unified LiteLLM complete() / embed() with retry, timeout, optional cache."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import litellm
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from repo2resume.config import AppConfig
from repo2resume.storage.cache import CacheBackend

logger = logging.getLogger(__name__)

LLM_CACHE_TTL = 7 * 24 * 3600  # 7 days

_QUOTA_TOKENS = (
    "quota",
    "balance",
    "insufficient",
    "billing",
    "credit",
    "payment required",
    "额度",
    "欠费",
    "余额不足",
    "资源包",
    "套餐已用完",
    "exceeded your current quota",
    "account balance",
)


@dataclass
class CompletionResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None


def _is_retryable(exc: BaseException) -> bool:
    if _is_quota_exhausted(exc):
        return False
    status = getattr(exc, "status_code", None)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    tokens = ("timeout", "ratelimit", "serviceunavailable", "apiconnection")
    return any(token in name for token in tokens)


def _is_quota_exhausted(exc: BaseException) -> bool:
    """Paid quota / balance errors that should trigger fallback model."""
    status = getattr(exc, "status_code", None)
    if status in {402, 403}:
        return True
    text = str(exc).lower()
    return any(token in text for token in _QUOTA_TOKENS)


class LLMClient:
    def __init__(self, config: AppConfig, cache: CacheBackend | None = None) -> None:
        self._config = config
        self._cache = cache
        litellm.drop_params = True

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        use_cache: bool = True,
    ) -> CompletionResult:
        resolved = model or self._config.llm_model
        cache_key = self._cache_key("llm", resolved, messages, temperature)
        if use_cache and self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                data = json.loads(cached)
                return CompletionResult(**data)

        try:
            response = self._completion_with_retry(
                model=resolved,
                messages=messages,
                temperature=temperature,
            )
            used_model = resolved
        except Exception as exc:
            fallback = self._config.llm_fallback_model
            if (
                fallback
                and fallback != resolved
                and model is None  # only auto-fallback for default path
                and _is_quota_exhausted(exc)
            ):
                logger.warning(
                    "Primary model %s quota/balance exhausted (%s); falling back to %s",
                    resolved,
                    exc,
                    fallback,
                )
                response = self._completion_with_retry(
                    model=fallback,
                    messages=messages,
                    temperature=temperature,
                )
                used_model = fallback
            else:
                raise
        choice = response.choices[0].message
        content = choice.content or ""
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cost: float | None
        try:
            cost = float(litellm.completion_cost(completion_response=response))
        except Exception:  # noqa: BLE001
            cost = None

        result = CompletionResult(
            content=content,
            model=used_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        if use_cache and self._cache is not None:
            # Cache under the model that actually answered.
            key = self._cache_key("llm", used_model, messages, temperature)
            self._cache.set(key, json.dumps(result.__dict__), ttl=LLM_CACHE_TTL)
        return result

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, list[Any]]:
        """LLM call with function-calling tools. Returns (content, raw_tool_calls).

        raw_tool_calls: litellm tool_call objects (have .id and .function.{name,arguments}).
        Caller (agent adapter) converts them to its own ToolCall type.
        """
        resolved = model or self._config.llm_model
        response = self._completion_with_retry(
            model=resolved,
            messages=messages,
            temperature=temperature,
            tools=tools,
        )
        choice = response.choices[0].message
        content = choice.content or ""
        raw_tool_calls = getattr(choice, "tool_calls", None) or []
        return content, list(raw_tool_calls)

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        resolved = model or self._config.model_for("embed")
        response = self._embedding_with_retry(model=resolved, input=texts)
        data = sorted(response.data, key=lambda item: item["index"])
        return [list(item["embedding"]) for item in data]

    def _api_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self._config.llm_timeout_s}
        if self._config.llm_api_key:
            kwargs["api_key"] = self._config.llm_api_key
        return kwargs

    def _completion_with_retry(self, **kwargs: Any) -> Any:
        attempts = max(1, self._config.llm_max_retries)

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception(_is_retryable),
        )
        def _call() -> Any:
            return litellm.completion(**kwargs, **self._api_kwargs())

        return _call()

    def _embedding_with_retry(self, **kwargs: Any) -> Any:
        attempts = max(1, self._config.llm_max_retries)

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception(_is_retryable),
        )
        def _call() -> Any:
            return litellm.embedding(**kwargs, **self._api_kwargs())

        return _call()

    @staticmethod
    def _cache_key(
        prefix: str,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
    ) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages, "temperature": temperature},
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{prefix}:{model}:{digest}"
