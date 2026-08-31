"""四桶归因：model / prompt / tool / business。规则表，不上分类器。"""

from __future__ import annotations


def infer_bucket(
    *,
    error: BaseException | None = None,
    error_type: str | None = None,
    stop_reason: str | None = None,
    hitl: str | None = None,
    stage: str | None = None,
) -> str:
    """返回 bucket 名。"""
    et = error_type or (type(error).__name__ if error is not None else "")
    if et in {"TimeoutError", "FuturesTimeout"} or "timeout" in et.lower():
        return "model"
    if hitl == "denied":
        return "business"
    if stop_reason in {"stuck", "max_rounds"}:
        return "prompt"
    if stage in {"hard_filter", "campus_gate", "snippet_jd", "jobs.filter"}:
        return "business"
    if error is not None or (et and et not in {"", "None"}):
        return "tool"
    if stop_reason in {"cancelled"}:
        return "tool"
    return "business"
