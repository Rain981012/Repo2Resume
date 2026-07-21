"""【手写】TDD：实现 agent/prompt_assembler.py 各空后，测试应变绿。"""

from __future__ import annotations

from repo2resume.agent.prompt_assembler import PromptAssembler
from repo2resume.agent.tools import ToolRegistry, make_add_tool


def _registry_with_add() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(make_add_tool())
    return reg


def test_render_state_empty_returns_empty() -> None:
    asm = PromptAssembler(base="B", registry=ToolRegistry())
    assert asm._render_state(None) == ""
    assert asm._render_state({}) == ""


def test_render_state_lines() -> None:
    asm = PromptAssembler(base="B", registry=ToolRegistry())
    out = asm._render_state({"phase": "analyze", "done": ["mine"]})
    assert "phase: analyze" in out
    assert "done: ['mine']" in out


def test_render_tools_lists_registered() -> None:
    asm = PromptAssembler(base="B", registry=_registry_with_add())
    out = asm._render_tools()
    assert "- add: Add two integers." in out


def test_render_tools_empty_registry() -> None:
    asm = PromptAssembler(base="B", registry=ToolRegistry())
    assert asm._render_tools() == ""


def test_build_assembles_all_sections() -> None:
    asm = PromptAssembler(base="你是助手", registry=_registry_with_add())
    system = asm.build(state={"phase": "analyze"})
    assert system.startswith("你是助手")
    assert "[当前状态]" in system
    assert "phase: analyze" in system
    assert "[可用工具]" in system
    assert "- add: Add two integers." in system


def test_build_omits_empty_sections() -> None:
    asm = PromptAssembler(base="你是助手", registry=ToolRegistry())
    system = asm.build(state=None)
    assert system == "你是助手"  # 没状态、没工具，只剩 base
