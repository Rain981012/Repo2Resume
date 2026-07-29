"""统一 LLM 客户端：基于 LiteLLM 封装 `complete()` / `complete_with_tools()` / `embed()`。

带指数退避重试（仅对超时/限流/5xx 重试，配额耗尽不重试）、可选缓存（按 model+messages+
temperature hash，TTL 7 天）、主模型配额耗尽时自动切 fallback 模型。`complete_with_tools`
是 Phase 2 给 agent loop 用的 function-calling 入口，返回原生 tool_call 对象由 adapter 翻译。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
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


@dataclass
class LLMUsage:
    """一次 LLM 调用的用量/成本/耗时摘要，供 adapter 落 trace。

    complete_with_tools 返回它（方案 2），由 adapter 决定是否调 trace_sink 落库，
    使 LLMClient 本身不依赖 db，保持可被非 agent 调用方（pipeline/profiler）复用。
    """

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试：配额耗尽不重试；超时/限流/5xx/连接错误才重试。

    注意：我们自己的硬 TimeoutError **不重试**——重试只会把「慢但能成」的
    Writer 调用砍成 60s×3 次失败，体感更慢且更易全盘失败。
    """
    if isinstance(exc, TimeoutError):
        return False
    if _is_quota_exhausted(exc):
        return False
    status = getattr(exc, "status_code", None)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    # 排除内置 TimeoutError（上面已处理）；仍重试 httpx/openai 类超时名
    if name == "timeouterror":
        return False
    tokens = ("ratelimit", "serviceunavailable", "apiconnection")
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
        # 关掉 LiteLLM 自带刷屏（completion / success_handler）；需要排查时用 --verbose
        litellm.set_verbose = False
        litellm.suppress_debug_info = True
        litellm.turn_off_message_logging = True
        for name in ("LiteLLM", "litellm", "LiteLLM Router", "LiteLLM Proxy"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.CRITICAL)
            lg.handlers.clear()
            lg.propagate = False

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        use_cache: bool = True,
        timeout_s: float | None = None,
    ) -> CompletionResult:
        """普通补全：先查缓存 → 调 LiteLLM（带重试）→ 主模型配额耗尽则切 fallback → 写缓存。

        timeout_s: 覆盖 config.llm_timeout_s；Writer/Critic 应传更长（如 180）。
        """
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
                _timeout_s=timeout_s,
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
                    _timeout_s=timeout_s,
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
    ) -> tuple[str, list[Any], LLMUsage]:
        """LLM call with function-calling tools. Returns (content, raw_tool_calls, usage).

        raw_tool_calls: litellm tool_call objects (have .id and .function.{name,arguments}).
        usage: LLMUsage(model, input_tokens, output_tokens, cost_usd, latency_ms) —— 方案 2，
            由 adapter 决定是否调 trace_sink 落库，LLMClient 本身不碰 db。
        """
        resolved = model or self._config.llm_model
        t0 = time.perf_counter()
        response = self._completion_with_retry(
            model=resolved,
            messages=messages,
            temperature=temperature,
            tools=tools,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        choice = response.choices[0].message
        content = choice.content or ""
        raw_tool_calls = getattr(choice, "tool_calls", None) or []
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cost: float | None
        try:
            cost = float(litellm.completion_cost(completion_response=response))
        except Exception:  # noqa: BLE001
            cost = None
        return (
            content,
            list(raw_tool_calls),
            LLMUsage(
                model=resolved,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
            ),
        )

    def complete_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        on_token: Callable[[str], None] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, list[Any], LLMUsage]:
        """流式版 complete_with_tools：每个文本 chunk 调 on_token(text)，返回同结构的累积结果。

        为什么单独一个方法而不是给 complete_with_tools 加 stream 参数？
          - 流式需要 yield/回调，返回值结构虽一样但调用方语义不同（要处理 token 回调）。
          - 非流式走缓存 + fallback；流式暂不走缓存（流式缓存意义不大，且 fallback 逻辑
            在流式下要中途切换模型，复杂度高，MVP 先不做）。
        """
        from types import SimpleNamespace

        resolved = model or self._config.llm_model
        t0 = time.perf_counter()
        response = litellm.completion(
            model=resolved,
            messages=messages,
            temperature=temperature,
            tools=tools,
            stream=True,
            **self._api_kwargs(),
        )
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage_obj: Any = None
        for chunk in response:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_obj = chunk.usage
                continue
            delta = choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                if on_token:
                    on_token(text)
            tc_chunks = getattr(delta, "tool_calls", None) or []
            for tc in tc_chunks:
                idx = tc.index if tc.index is not None else len(tool_acc)
                if idx not in tool_acc:
                    tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.id:
                    tool_acc[idx]["id"] = tc.id
                fn = tc.function
                if fn:
                    if fn.name:
                        tool_acc[idx]["name"] += fn.name
                    if fn.arguments:
                        tool_acc[idx]["arguments"] += fn.arguments
            if hasattr(chunk, "usage") and chunk.usage:
                usage_obj = chunk.usage

        raw_tool_calls = [
            SimpleNamespace(
                id=tool_acc[idx]["id"],
                function=SimpleNamespace(
                    name=tool_acc[idx]["name"],
                    arguments=tool_acc[idx]["arguments"] or "{}",
                ),
            )
            for idx in sorted(tool_acc)
        ]
        latency_ms = int((time.perf_counter() - t0) * 1000)
        input_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        cost: float | None
        try:
            # 流式没有完整 response 对象，用 token 数估算成本
            cost = float(
                litellm.completion_cost(
                    model=resolved,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                )
            )
        except Exception:  # noqa: BLE001
            cost = None
        return (
            "".join(content_parts),
            raw_tool_calls,
            LLMUsage(
                model=resolved,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
            ),
        )

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
        """用 tenacity 包一层 `litellm.completion`：指数退避 + 仅对可重试异常重试。

        额外用 Future.result(timeout=…) 做硬超时：部分 provider（如 zai）可能忽略
        litellm 的 timeout 参数，导致 Writer 卡十几分钟。
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        attempts = max(1, self._config.llm_max_retries)
        override = kwargs.pop("_timeout_s", None)
        timeout_s = float(
            override if override is not None else self._config.llm_timeout_s
        )

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception(_is_retryable),
        )
        def _call() -> Any:
            call_kwargs = {**kwargs, **self._api_kwargs()}
            # 与硬超时对齐，避免 litellm 内部超时更短/更长不一致
            call_kwargs["timeout"] = timeout_s

            def _invoke() -> Any:
                return litellm.completion(**call_kwargs)

            pool = ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(_invoke)
                from repo2resume.agent.cancel import RunCancelled
                from repo2resume.agent.progress import emit_wait_tick, heartbeat_wait_slice

                slice_s = heartbeat_wait_slice()
                if slice_s <= 0:
                    try:
                        return fut.result(timeout=timeout_s)
                    except FuturesTimeout as exc:
                        raise TimeoutError(
                            f"LLM 调用超过 {timeout_s:.0f}s（model={call_kwargs.get('model')}）"
                        ) from exc

                # chat 心跳开启：主线程每 slice_s 醒一次并打印（后台线程写 stdout 不可靠）
                deadline = time.monotonic() + timeout_s
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"LLM 调用超过 {timeout_s:.0f}s（model={call_kwargs.get('model')}）"
                        )
                    try:
                        return fut.result(timeout=min(slice_s, remaining))
                    except FuturesTimeout:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"LLM 调用超过 {timeout_s:.0f}s（model={call_kwargs.get('model')}）"
                            ) from None
                        try:
                            emit_wait_tick()
                        except RunCancelled:
                            raise
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

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
