"""obs_run_id：主 turn 发号，嵌套子 loop 复用。"""

import repo2resume.observability.run_context as rc
from repo2resume.observability.run_context import (
    current_run_id,
    enter_agent_turn,
    exit_agent_turn,
)


def _reset() -> None:
    rc._turn_depth.set(0)
    rc._obs_run_id.set(None)


def test_nested_turns_share_run_id() -> None:
    _reset()
    parent = enter_agent_turn()
    assert parent.startswith("turn-")
    child = enter_agent_turn()
    assert child == parent
    exit_agent_turn()
    assert current_run_id() == parent
    exit_agent_turn()
    assert current_run_id() == parent


def test_sequential_top_level_turns_get_new_ids() -> None:
    _reset()
    a = enter_agent_turn()
    exit_agent_turn()
    b = enter_agent_turn()
    exit_agent_turn()
    assert a != b
    assert current_run_id() == b
