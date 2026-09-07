"""Loop detection tests: tool repetition + semantic repetition."""
from __future__ import annotations

import pytest

from sera_browser.models import BrowserState
from sera_browser.task.loop import LoopRecoveryManager


def test_tool_repetition_detected():
    loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
    loop.record_tool("click", "ref=button#1:Login")
    loop.record_tool("click", "ref=button#1:Login")
    assert loop.tool_loop() is None  # only 2 so far
    loop.record_tool("click", "ref=button#1:Login")
    assert loop.tool_loop() is not None


def test_tool_repetition_reset_after_other_action():
    loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
    loop.record_tool("click", "ref=button#1:Login")
    loop.record_tool("click", "ref=button#1:Login")
    loop.record_tool("navigate", "url=/")  # different action breaks the run
    loop.record_tool("click", "ref=button#1:Login")
    assert loop.tool_loop() is None


def test_semantic_loop_detected():
    loop = LoopRecoveryManager(tool_repeat_limit=4, semantic_loop_limit=3)
    state = BrowserState(task_id="t", target="http://x")
    loop.record_dom_hash("hash-A")
    loop.record_dom_hash("hash-A")
    assert loop.semantic_loop(state) is False  # only 2 so far
    loop.record_dom_hash("hash-A")
    assert loop.semantic_loop(state) is True


def test_semantic_loop_not_detected_when_changes():
    loop = LoopRecoveryManager(tool_repeat_limit=4, semantic_loop_limit=3)
    state = BrowserState(task_id="t", target="http://x")
    loop.record_dom_hash("hash-A")
    loop.record_dom_hash("hash-B")
    loop.record_dom_hash("hash-A")
    assert loop.semantic_loop(state) is False


def test_recovery_suggestion_changes():
    loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
    state = BrowserState(task_id="t", target="http://x")
    assert loop.suggest_recovery(state) == "continue"
    loop.record_tool("click", "ref=b")
    loop.record_tool("click", "ref=b")
    loop.record_tool("click", "ref=b")
    # v4: every loop signal maps to target abandonment — the caller must
    # select a different unexplored target, never a screenshot.
    assert loop.suggest_recovery(state) == "abandon_target"
    # And the escape blocks the repeated target.
    signal = loop.escape(state)
    assert signal == "tool_repeat"
    assert "ref=b" in loop.blocked_refs
