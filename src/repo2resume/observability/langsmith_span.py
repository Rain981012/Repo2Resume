"""LangSmith 可选 span：未设环境变量或未安装 SDK 时为零开销 no-op。"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
T = TypeVar("T")


def tracing_enabled() -> bool:
    flag = os.environ.get("LANGSMITH_TRACING", "").strip().lower()
    if flag not in _TRUTHY:
        return False
    key = os.environ.get("LANGSMITH_API_KEY", "").strip()
    return key.startswith("lsv2_")


def _clip(value: Any, *, limit: int = 400) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit=limit) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_clip(v, limit=limit) for v in value[:8]]
    return value


def _preview(value: Any, *, limit: int = 400) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _load_trace() -> Any:
    """单独抽出以便单测 mock；生产里才 import langsmith。"""
    from langsmith import trace

    return trace


@contextmanager
def langsmith_span(
    name: str,
    *,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[Any]:
    """嵌套 span；外层 agent.turn 包住一整句用户输入。"""
    if not tracing_enabled():
        yield None
        return
    try:
        trace = _load_trace()
    except ImportError:
        logger.warning("LANGSMITH_TRACING=true 但未安装 langsmith：pip install langsmith")
        yield None
        return
    payload = _clip(inputs or {})
    extra: dict[str, Any] = {}
    if metadata:
        extra["metadata"] = metadata
    if tags:
        extra["tags"] = tags
    try:
        with trace(name=name, run_type=run_type, inputs=payload, **extra) as run:
            yield run
    except TypeError:
        with trace(name, run_type=run_type, inputs=payload) as run:
            yield run


def finish_span(
    span: Any,
    *,
    outputs: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """把输出/错误写进 span；no-op 时 span 为 None。"""
    if span is None:
        return
    end = getattr(span, "end", None)
    if not callable(end):
        return
    kwargs: dict[str, Any] = {}
    if outputs is not None:
        kwargs["outputs"] = _clip(outputs)
    if error is not None:
        kwargs["error"] = f"{type(error).__name__}: {error}"
    try:
        end(**kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("langsmith span.end failed", exc_info=True)


def span_call(
    name: str,
    fn: Callable[[], T],
    *,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    outputs_of: Callable[[T], dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> T:
    """执行 fn，把 return / 异常写进 LangSmith Output。`trace()` 不会自动记 Python 返回值。"""
    with langsmith_span(
        name,
        run_type=run_type,
        inputs=inputs,
        metadata=metadata,
        tags=tags,
    ) as span:
        try:
            result = fn()
        except BaseException as exc:
            finish_span(span, error=exc)
            raise
        if outputs_of is not None:
            try:
                outputs = outputs_of(result)
            except Exception:  # noqa: BLE001
                outputs = {"preview": _preview(result)}
        else:
            outputs = {"preview": _preview(result)}
        finish_span(span, outputs=outputs)
        return result
