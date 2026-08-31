"""卡死停机时的可读原因：给 LangSmith Output 和用户看，不只写「重复调用」。"""

from __future__ import annotations

from typing import Any

_CLUES: tuple[tuple[str, str], ...] = (
    ("【需先 set_job_prefs】", "缺搜岗偏好，应先 set_job_prefs 而不是重试 job_scout"),
    ("判定卡死", "内层子 agent 已经卡死，外层再调同一工具不会好转"),
    ("最大轮数", "内层达到最大轮数，返回里通常没有完成标记"),
    ("未找到仍在招", "搜岗空结果；若未带完成标记，主模型会当成失败再搜"),
    ("timeout", "上次调用超时"),
    ("超时", "上次调用超时"),
    ("Traceback", "上次工具抛了异常"),
    ("PermissionDenied", "权限拒绝"),
    ("用户拒绝", "HITL 拒绝执行"),
)


def explain_stuck(
    *,
    sig: tuple[str, ...],
    repeat: int,
    tool_texts: list[str],
    early_markers: tuple[str, ...] = (),
) -> dict[str, Any]:
    last = str(tool_texts[-1] if tool_texts else "")
    markers = tuple(early_markers) if early_markers else ()
    missing = [m for m in markers if m and m not in last]
    why: list[str] = []
    if missing:
        why.append(
            "最近一次工具返回里没有完成标记（"
            + "、".join(missing)
            + "），主模型会当成没做完再调一次"
        )
    elif not last.strip():
        why.append("最近一次工具返回为空")
    else:
        why.append("工具有返回，但模型仍连打同名工具、没有改成最终回复")
    lowered = last.lower()
    for needle, label in _CLUES:
        if needle.lower() in lowered:
            why.append(label)
            break
    return {
        "tools": list(sig),
        "repeat": int(repeat),
        "missing_markers": missing,
        "last_tool_preview": last[:400],
        "why": "；".join(why),
    }


def format_stuck_message(detail: dict[str, Any]) -> str:
    tools = tuple(detail.get("tools") or ())
    repeat = detail.get("repeat") or 0
    lines = [
        f"[检测到连续 {repeat} 次重复调用 {tools}，判定卡死，停止]",
        f"原因：{detail.get('why') or '连续同名工具'}",
    ]
    preview = (detail.get("last_tool_preview") or "").strip()
    if preview:
        one_line = " ".join(preview.split())
        if len(one_line) > 240:
            one_line = one_line[:240] + "…"
        lines.append(f"最近一次工具返回：{one_line}")
    return "\n".join(lines)
