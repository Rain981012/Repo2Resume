from repo2resume.observability.stuck import explain_stuck, format_stuck_message


def test_explain_stuck_missing_job_scout_marker() -> None:
    detail = explain_stuck(
        sig=("job_scout",),
        repeat=3,
        tool_texts=["子代理还在想，没有列表"],
        early_markers=("【job_scout已完成】",),
    )
    assert detail["tools"] == ["job_scout"]
    assert detail["repeat"] == 3
    assert "【job_scout已完成】" in detail["missing_markers"]
    assert "完成标记" in detail["why"]
    msg = format_stuck_message(detail)
    assert "卡死" in msg
    assert "原因：" in msg
    assert "最近一次工具返回" in msg


def test_explain_stuck_sees_timeout_clue() -> None:
    detail = explain_stuck(
        sig=("job_scout",),
        repeat=3,
        tool_texts=["LLM timeout after 60s"],
        early_markers=("【job_scout已完成】",),
    )
    assert "超时" in detail["why"]
