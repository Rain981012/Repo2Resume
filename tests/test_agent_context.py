"""【手写】TDD：实现 agent/context.py 各空后，测试应变绿。"""

from __future__ import annotations

from repo2resume.agent.context import ContextManager


def _fake_counter(messages):
    # 每条消息算 10 token，方便构造超预算场景
    return len(messages) * 10


def test_add_and_messages_include_system() -> None:
    ctx = ContextManager(system="你是助手", count_tokens=_fake_counter)
    ctx.add("user", "hi")
    msgs = ctx.messages()
    assert msgs[0] == {"role": "system", "content": "你是助手"}
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_add_tool_result_shape() -> None:
    ctx = ContextManager(system="s", count_tokens=_fake_counter)
    ctx.add_tool_result(tool_call_id="call_1", content="42")
    tool_msg = ctx.messages()[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["content"] == "42"


def test_token_count_uses_injected_counter() -> None:
    ctx = ContextManager(system="s", count_tokens=_fake_counter)
    ctx.add("user", "a")
    ctx.add("assistant", "b")
    # system + 2 messages = 3 条 → 30 token
    assert ctx.token_count() == 30


def test_maybe_compact_no_op_when_under_budget() -> None:
    ctx = ContextManager(system="s", max_tokens=100, count_tokens=_fake_counter)
    ctx.add("user", "a")
    assert ctx.maybe_compact() is False
    assert len(ctx.messages()) == 2  # system + user


def test_maybe_compact_trims_when_over_budget() -> None:
    ctx = ContextManager(
        system="s",
        max_tokens=30,
        count_tokens=_fake_counter,
    )
    # 加 10 条 user 消息 → system + 10 = 11 条 = 110 token，超 30
    for i in range(10):
        ctx.add("user", f"m{i}")
    compacted = ctx.maybe_compact(keep_last=4)
    assert compacted is True
    msgs = ctx.messages()
    # system(1) + 裁剪标记(1) + 保留 4 条 = 6
    assert len(msgs) == 6
    assert msgs[1]["role"] == "system"  # 裁剪标记
    assert "裁剪" in msgs[1]["content"]
    # 最后一条仍是最新消息
    assert msgs[-1]["content"] == "m9"


def test_compact_never_leaves_orphan_tool_message() -> None:
    """按条裁可能切掉 assistant(tool_calls)，留下配对不上的 tool 消息。"""
    ctx = ContextManager(system="s", max_tokens=10, count_tokens=_fake_counter)
    ctx.add("user", "hi")
    ctx.add(
        "assistant",
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
        ],
    )
    ctx.add_tool_result("call_1", "result")
    ctx.add("assistant", "done")

    ctx.maybe_compact(keep_last=2)

    msgs = ctx.messages()
    ids = {tc["id"] for m in msgs if m.get("tool_calls") for tc in m["tool_calls"]}
    for m in msgs:
        if m["role"] == "tool":
            assert m["tool_call_id"] in ids


def test_compact_keeps_consecutive_tool_results() -> None:
    ctx = ContextManager(system="s", max_tokens=10, count_tokens=_fake_counter)
    ctx.add("user", "hi")
    ctx.add(
        "assistant",
        "",
        tool_calls=[
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
            {"id": "c", "type": "function", "function": {"name": "z", "arguments": "{}"}},
        ],
    )
    ctx.add_tool_result("a", "ra")
    ctx.add_tool_result("b", "rb")
    ctx.add_tool_result("c", "rc")
    ctx.add("assistant", "done")

    ctx.maybe_compact(keep_last=2)

    msgs = ctx.messages()
    ids = {tc["id"] for m in msgs if m.get("tool_calls") for tc in m["tool_calls"]}
    tool_ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert tool_ids == ["a", "b", "c"]
    assert ids == {"a", "b", "c"}
