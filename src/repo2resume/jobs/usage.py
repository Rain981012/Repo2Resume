"""搜岗 API（Bocha / Tavily）用量统计，供 chat `/cost` 展示。"""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchApiCall:
    provider: str
    ok: bool
    latency_ms: int
    results: int = 0
    # Tavily basic ≈ 1 credit；Bocha 按次计，未知单价时用 credits 记次数
    credits: float = 1.0
    note: str = ""


@dataclass
class SearchUsageTracker:
    calls: list[SearchApiCall] = field(default_factory=list)

    def record(
        self,
        provider: str,
        *,
        ok: bool,
        latency_ms: int,
        results: int = 0,
        credits: float = 1.0,
        note: str = "",
    ) -> None:
        self.calls.append(
            SearchApiCall(
                provider=provider,
                ok=ok,
                latency_ms=latency_ms,
                results=results,
                credits=credits,
                note=note,
            )
        )

    def stats(self) -> dict[str, Any]:
        per: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            slot = per.setdefault(
                c.provider,
                {"count": 0, "ok": 0, "errors": 0, "latency_ms": 0, "results": 0, "credits": 0.0},
            )
            slot["count"] += 1
            slot["ok"] += 1 if c.ok else 0
            slot["errors"] += 0 if c.ok else 1
            slot["latency_ms"] += c.latency_ms
            slot["results"] += c.results
            slot["credits"] += c.credits
        return {
            "count": len(self.calls),
            "total_latency_ms": sum(c.latency_ms for c in self.calls),
            "total_credits": sum(c.credits for c in self.calls),
            "per_provider": per,
        }


_tracker: ContextVar[SearchUsageTracker | None] = ContextVar(
    "repo2resume_search_usage",
    default=None,
)


def set_search_usage_tracker(tracker: SearchUsageTracker | None) -> Token:
    return _tracker.set(tracker)


def reset_search_usage_tracker(token: Token) -> None:
    _tracker.reset(token)


def record_search_api(
    provider: str,
    *,
    ok: bool,
    latency_ms: int,
    results: int = 0,
    credits: float = 1.0,
    note: str = "",
) -> None:
    tr = _tracker.get()
    if tr is not None:
        tr.record(
            provider,
            ok=ok,
            latency_ms=latency_ms,
            results=results,
            credits=credits,
            note=note,
        )


class timed_search_call:
    """with timed_search_call('bocha') as t: ...; t.finish(ok=True, results=n)"""

    def __init__(self, provider: str, *, credits: float = 1.0) -> None:
        self.provider = provider
        self.credits = credits
        self._t0 = 0.0
        self.latency_ms = 0

    def __enter__(self) -> timed_search_call:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def finish(self, *, ok: bool, results: int = 0, note: str = "") -> None:
        self.latency_ms = int((time.perf_counter() - self._t0) * 1000)
        record_search_api(
            self.provider,
            ok=ok,
            latency_ms=self.latency_ms,
            results=results,
            credits=self.credits,
            note=note,
        )
