"""统一 LLM 客户端：基于 LiteLLM 封装 `complete()` / `complete_with_tools()` / `embed()`。

带指数退避重试（仅对超时/限流/5xx 重试，配额耗尽不重试）、可选缓存（按 model+messages+
temperature hash，TTL 7 天）、主模型配额耗尽时自动切 fallback 模型。`complete_with_tools`
是 Phase 2 给 agent loop 用的 function-calling 入口，返回原生 tool_call 对象由 adapter 翻译。
"""

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
    """一次补全的结果：文本、所用模型、输入/输出 token 数、可选成本（美元）。"""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试：配额耗尽不重试；超时/限流/5xx/连接错误才重试。"""
    if _is_quota_exhausted(exc):
        return False
    status = getattr(exc, "status_code", None)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    tokens = ("timeout", "ratelimit", "serviceunavailable", "apiconnection")
    return any(token in name for token in tokens)


def _is_quota_exhausted(exc: BaseException) -> bool:
    """识别付费配额/余额耗尽错误（402/403 或错误文本含额度关键词），用于触发 fallback。"""
    status = getattr(exc, "status_code", None)
    if status in {402, 403}:
        return True
    text = str(exc).lower()
    return any(token in text for token in _QUOTA_TOKENS)


class LLMClient:
    """LiteLLM 封装：补全/工具调用/嵌入，带重试、缓存、fallback。"""

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
        """普通补全：先查缓存 → 调 LiteLLM（带重试）→ 主模型配额耗尽则切 fallback → 写缓存。"""
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
        """批量文本嵌入：按 index 排序保证返回顺序与输入一致。"""
        resolved = model or self._config.model_for("embed")
        response = self._embedding_with_retry(model=resolved, input=texts)
        data = sorted(response.data, key=lambda item: item["index"])
        return [list(item["embedding"]) for item in data]

    def _api_kwargs(self) -> dict[str, Any]:
        """组装传给 LiteLLM 的公共参数：超时 + 可选 api_key。"""
        kwargs: dict[str, Any] = {"timeout": self._config.llm_timeout_s}
        if self._config.llm_api_key:
            kwargs["api_key"] = self._config.llm_api_key
        return kwargs

    def _completion_with_retry(self, **kwargs: Any) -> Any:
        """用 tenacity 包一层 `litellm.completion`：指数退避 + 仅对可重试异常重试。"""
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
        """`litellm.embedding` 的重试封装，策略同 `_completion_with_retry`。"""
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
        """按 model+messages+temperature 的 sha256 生成缓存键，相同请求命中同一结果。"""
        payload = json.dumps(
            {"model": model, "messages": messages, "temperature": temperature},
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{prefix}:{model}:{digest}"
