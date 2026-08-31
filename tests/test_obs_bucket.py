from repo2resume.observability.bucket import infer_bucket


def test_timeout_is_model() -> None:
    assert infer_bucket(error=TimeoutError("x")) == "model"
    assert infer_bucket(error_type="TimeoutError") == "model"


def test_stuck_and_max_rounds_are_prompt() -> None:
    assert infer_bucket(stop_reason="stuck") == "prompt"
    assert infer_bucket(stop_reason="max_rounds") == "prompt"


def test_tool_exception_is_tool() -> None:
    assert infer_bucket(error=RuntimeError("boom")) == "tool"


def test_hitl_and_hard_filter_are_business() -> None:
    assert infer_bucket(hitl="denied") == "business"
    assert infer_bucket(stage="jobs.filter") == "business"
    assert infer_bucket(stage="snippet_jd") == "business"


def test_default_success_is_business() -> None:
    assert infer_bucket(stop_reason="final_text") == "business"
