"""Tests for agent/progress heartbeat + emit."""

from __future__ import annotations

import time

from repo2resume.agent.progress import (
    emit_progress,
    emit_wait_tick,
    heartbeat_paused,
    heartbeat_wait_slice,
    progress_heartbeat,
    reset_progress_callback,
    set_progress_callback,
)


def test_emit_progress_invokes_callback():
    seen: list[str] = []
    token = set_progress_callback(seen.append)
    try:
        emit_progress("step-a")
        emit_progress("")
    finally:
        reset_progress_callback(token)
    assert seen == ["step-a"]


def test_progress_heartbeat_emits_initial_and_enables_slice():
    seen: list[str] = []
    token = set_progress_callback(seen.append)
    try:
        assert heartbeat_wait_slice() == 0.0
        with progress_heartbeat(10.0):
            assert seen == ["处理中…"]
            assert heartbeat_wait_slice() == 10.0
            emit_progress("分析仓库中…")
            assert seen[-1] == "分析仓库中…"
            time.sleep(0.01)
            emit_wait_tick()
            assert any("分析仓库中…（已等待" in m for m in seen)
    finally:
        reset_progress_callback(token)
    assert heartbeat_wait_slice() == 0.0


def test_heartbeat_paused_skips_wait_tick():
    seen: list[str] = []
    token = set_progress_callback(seen.append)
    try:
        with progress_heartbeat(10.0):
            with heartbeat_paused():
                emit_wait_tick()
            assert seen == ["处理中…"]
            emit_wait_tick()
            assert any("已等待" in m for m in seen)
    finally:
        reset_progress_callback(token)


def test_emit_progress_reentrant_under_heartbeat_wrapper():
    """RLock：heartbeat 包装后 emit_progress 不得自锁死。"""
    seen: list[str] = []
    token = set_progress_callback(seen.append)
    try:
        with progress_heartbeat(10.0):
            emit_progress("ok")
    finally:
        reset_progress_callback(token)
    assert "ok" in seen
