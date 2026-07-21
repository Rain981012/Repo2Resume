"""【手写】TDD：实现 agent/tools.py 各空后，测试应变绿。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from repo2resume.agent.tools import Tool, ToolRegistry, make_add_tool


class _EchoParams(BaseModel):
    text: str


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text back.",
        params_model=_EchoParams,
        handler=lambda text: text,
    )


def test_tool_json_schema_shape() -> None:
    tool = make_add_tool()
    schema = tool.json_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "add"
    assert "Add two integers" in schema["function"]["description"]
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "a" in params["properties"]
    assert "b" in params["properties"]


def test_tool_call_validates_and_runs() -> None:
    tool = make_add_tool()
    assert tool.call({"a": 2, "b": 3}) == 5


def test_tool_call_rejects_bad_args() -> None:
    tool = make_add_tool()
    with pytest.raises(ValidationError):
        tool.call({"a": "x", "b": 1})


def test_registry_register_list_and_call() -> None:
    reg = ToolRegistry()
    reg.register(make_add_tool())
    reg.register(_echo_tool())

    schemas = reg.list_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == {"add", "echo"}

    assert reg.call("add", {"a": 10, "b": 1}) == 11
    assert reg.call("echo", {"text": "hi"}) == "hi"


def test_registry_unknown_tool() -> None:
    reg = ToolRegistry()
    with pytest.raises((KeyError, ValueError)):
        reg.get("missing")
