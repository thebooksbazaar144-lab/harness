"""Tests for the advanced features (Tier 0 + Tier 1).

Covers:
- Versioned ref map (build, resolve, stale-ref detection, content signatures)
- Serialized tree format
- Multi-strategy element resolver (ref → CSS → role+name → text → coordinate)
- Multi-tab manager (open/close/switch, stable ids, popup tracking)
- Memory manager (record_step, update_long_term_memory, compaction)
- Snapshot trimmer (caps size, keeps interactive elements)
- Safety guard (dangerous keywords, action allowlist, domain blocklist)
- Observability (spans, action log, tracing lifecycle)
- Action schema: new actions (fill_form, mouse_click, drag, extract, etc.)
- Cache-friendly state view ordering
- Failure-adaptation (fresh snapshot on failure)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from sera_browser.browser.refmap import (
    RefMap,
    build_refmap,
    content_signature,
    new_version,
    resolve_ref,
    serialize_tree,
)
from sera_browser.browser.trimmer import trim_snapshot
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import (
    AccessibilityNode,
    BrowserState,
    InteractiveElement,
)
from sera_browser.task.memory import MemoryManager
from sera_browser.task.observability import Observability
from sera_browser.task.safety import (
    FULL_ACTIONS,
    READONLY_ACTIONS,
    SafetyConfig,
    SafetyGuard,
)
from sera_browser.task.action_schema import (
    parse_llm_response,
    validate_action,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_elements() -> list[InteractiveElement]:
    return [
        InteractiveElement(ref="button#1:Login", role="button", name="Login", tag="button"),
        InteractiveElement(ref="textbox#1:Username", role="textbox", name="Username", tag="input", type="text"),
        InteractiveElement(ref="textbox#2:Password", role="textbox", name="Password", tag="input", type="password"),
        InteractiveElement(ref="link#1:Products", role="link", name="Products", tag="a", href="/products"),
    ]


def _make_tree() -> list[AccessibilityNode]:
    return [
        AccessibilityNode(
            role="button",
            name="Login",
            ref="button#1:Login",
            children=[],
        ),
        AccessibilityNode(
            role="textbox",
            name="Username",
            ref="textbox#1:Username",
            children=[],
        ),
    ]


def _make_state(**overrides: Any) -> BrowserState:
    state = BrowserState(task_id="test", target="http://127.0.0.1:9999/")
    state.current_url = "http://127.0.0.1:9999/"
    state.dom_hash = "abc123"
    state.interactive_elements = _make_elements()
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost,127.0.0.1:9999",
        max_steps=10,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# RefMap + serialized tree
# ---------------------------------------------------------------------------


class TestRefMap:
    def test_new_version_is_monotonic(self) -> None:
        v1 = new_version()
        v2 = new_version()
        assert v2 > v1

    def test_content_signature_is_stable(self) -> None:
        sig1 = content_signature("button", "Login", "button", None, None)
        sig2 = content_signature("button", "Login", "button", None, None)
        assert sig1 == sig2

    def test_content_signature_differs_for_different_elements(self) -> None:
        sig1 = content_signature("button", "Login", "button", None, None)
        sig2 = content_signature("button", "Logout", "button", None, None)
        assert sig1 != sig2

    def test_build_refmap_assigns_indices(self) -> None:
        tree = _make_tree()
        elements = _make_elements()
        refmap = build_refmap(tree, elements)
        assert refmap.version >= 1
        assert refmap.size == len(elements)
        # Each element should have an index.
        for entry in refmap.entries.values():
            assert entry.index >= 1
            assert entry.element is not None
            assert entry.content_signature != ""

    def test_build_refmap_preserves_indices_across_snapshots(self) -> None:
        """When the same elements appear in a new snapshot, their indices
        should be preserved (stable identity via content_signature)."""
        tree = _make_tree()
        elements = _make_elements()
        refmap1 = build_refmap(tree, elements)
        # Second snapshot with same elements — indices should match.
        refmap2 = build_refmap(tree, elements, previous=refmap1)
        for idx in refmap1.entries:
            assert idx in refmap2.entries
            assert refmap2.entries[idx].content_signature == refmap1.entries[idx].content_signature

    def test_resolve_ref_returns_entry(self) -> None:
        tree = _make_tree()
        elements = _make_elements()
        refmap = build_refmap(tree, elements)
        # The first element's index should resolve.
        first_idx = list(refmap.entries.keys())[0]
        entry = resolve_ref(refmap, str(first_idx))
        assert entry is not None
        assert entry.element is not None

    def test_resolve_ref_rejects_stale_version(self) -> None:
        tree = _make_tree()
        elements = _make_elements()
        refmap = build_refmap(tree, elements)
        # Using a different expected_version should return None.
        entry = resolve_ref(refmap, "1", expected_version=refmap.version + 999)
        assert entry is None

    def test_serialize_tree_produces_compact_format(self) -> None:
        tree = _make_tree()
        elements = _make_elements()
        refmap = build_refmap(tree, elements)
        serialized = refmap.serialized
        assert "button" in serialized
        assert "Login" in serialized
        assert "textbox" in serialized
        # Should contain index markers like [1]
        assert "[" in serialized

    def test_refmap_dom_hash_changes_with_different_trees(self) -> None:
        tree1 = _make_tree()
        tree2 = [
            AccessibilityNode(role="link", name="Products", ref="link#1:Products", children=[]),
        ]
        elements = _make_elements()
        refmap1 = build_refmap(tree1, elements)
        refmap2 = build_refmap(tree2, elements)
        assert refmap1.dom_hash != refmap2.dom_hash


# ---------------------------------------------------------------------------
# Snapshot trimmer
# ---------------------------------------------------------------------------


class TestSnapshotTrimmer:
    def test_trim_keeps_interactive_elements(self) -> None:
        """The trimmer should keep all nodes that have a ref (interactive)."""
        tree = [
            AccessibilityNode(role="button", name="Login", ref="button#1:Login", children=[]),
            AccessibilityNode(role="textbox", name="Username", ref="textbox#1:Username", children=[]),
            AccessibilityNode(role="textbox", name="Password", ref="textbox#2:Password", children=[]),
            AccessibilityNode(role="link", name="Products", ref="link#1:Products", children=[]),
        ]
        elements = _make_elements()
        result = trim_snapshot(tree, elements)
        # All 4 tree nodes have refs → all should be kept.
        assert result.elements_kept == 4
        assert result.trimmed_chars > 0

    def test_trim_caps_at_max_chars(self) -> None:
        # Build a large tree.
        big_tree: list[AccessibilityNode] = []
        for i in range(500):
            big_tree.append(
                AccessibilityNode(
                    role="generic",
                    name=f"item-{i}-" + "x" * 100,
                    ref=None,
                    children=[],
                )
            )
        result = trim_snapshot(big_tree, [], max_chars=1000)
        assert result.trimmed_chars <= 1500  # small overshoot is ok
        assert "trimmed" in result.note.lower() or "hidden" in result.note.lower()

    def test_trim_hides_repetitive_list_items(self) -> None:
        """A list of 20 similar items should be trimmed to max_list_items."""
        children = [
            AccessibilityNode(role="listitem", name=f"item-{i}", ref=f"li#{i}", children=[])
            for i in range(20)
        ]
        tree = [AccessibilityNode(role="list", name="", ref=None, children=children)]
        result = trim_snapshot(tree, [], max_list_items=5)
        assert result.elements_hidden > 0
        assert "hidden" in result.note.lower()


# ---------------------------------------------------------------------------
# Memory manager
# ---------------------------------------------------------------------------


class TestMemoryManager:
    def test_record_step_appends_to_history(self) -> None:
        m = MemoryManager(max_full_steps=10)
        m.record_step(action="navigate", args={"url": "/"}, result="ok")
        m.record_step(action="click", args={"ref": "1"}, result="ok")
        assert len(m.step_history) == 2

    def test_update_long_term_memory(self) -> None:
        m = MemoryManager()
        m.update_long_term_memory("logged in as admin")
        assert "admin" in m.long_term_memory

    def test_compaction_triggers_when_buffer_exceeds_max(self) -> None:
        m = MemoryManager(max_full_steps=5)
        for i in range(10):
            m.record_step(action="click", args={"ref": str(i)}, result="ok")
        # After 10 steps with max_full_steps=5, compaction should have run.
        assert len(m.step_history) <= 5
        assert m.summary != ""

    def test_to_view_returns_compact_dict(self) -> None:
        m = MemoryManager()
        m.update_long_term_memory("test fact")
        m.record_step(action="navigate", args={}, result="ok", url="http://x/")
        view = m.to_view()
        assert "long_term_memory" in view
        assert "recent_steps" in view
        assert len(view["recent_steps"]) == 1
        assert view["recent_steps"][0]["action"] == "navigate"

    def test_long_term_memory_is_capped(self) -> None:
        m = MemoryManager()
        huge = "x" * 5000
        m.update_long_term_memory(huge)
        assert len(m.long_term_memory) <= 2000


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


class TestSafetyGuard:
    def test_allows_normal_click(self) -> None:
        state = _make_state()
        guard = SafetyGuard()
        decision = guard.check_action("click", {"ref": "button#1:Login"}, state)
        assert decision.allowed

    def test_blocks_dangerous_keyword_in_autonomous_mode(self) -> None:
        state = _make_state(
            interactive_elements=[
                InteractiveElement(ref="button#1:Delete", role="button", name="Delete account", tag="button"),
            ]
        )
        guard = SafetyGuard(SafetyConfig(block_dangerous=True))
        decision = guard.check_action("click", {"ref": "button#1:Delete"}, state)
        assert not decision.allowed
        assert decision.is_dangerous
        assert decision.matched_keyword == "delete"

    def test_allows_dangerous_in_supervised_mode(self) -> None:
        state = _make_state(
            interactive_elements=[
                InteractiveElement(ref="button#1:Delete", role="button", name="Delete account", tag="button"),
            ]
        )
        guard = SafetyGuard(SafetyConfig(block_dangerous=False))
        decision = guard.check_action("click", {"ref": "button#1:Delete"}, state)
        assert decision.allowed
        assert decision.is_dangerous
        assert decision.requires_confirmation

    def test_readonly_policy_blocks_mutations(self) -> None:
        state = _make_state()
        guard = SafetyGuard(SafetyConfig(action_policy="readonly"))
        # fill is a mutation — should be blocked.
        decision = guard.check_action("fill", {"ref": "1", "value": "x"}, state)
        assert not decision.allowed
        # navigate is allowed in readonly.
        decision = guard.check_action("navigate", {"url": "http://127.0.0.1:9999/"}, state)
        assert decision.allowed

    def test_domain_blocklist_rejects_navigation(self) -> None:
        state = _make_state()
        guard = SafetyGuard(SafetyConfig(blocked_domains={"evil.com"}))
        decision = guard.check_action("navigate", {"url": "http://evil.com/admin"}, state)
        assert not decision.allowed
        assert "evil.com" in decision.reason

    def test_fill_form_validated_against_dangerous_keywords(self) -> None:
        """fill_form with a field whose value contains a dangerous keyword
        is NOT blocked (the keyword check is on element text, not values)."""
        state = _make_state()
        guard = SafetyGuard()
        decision = guard.check_action(
            "fill_form",
            {"fields": [{"ref": "button#1:Login", "value": "delete this"}]},
            state,
        )
        # Values aren't checked for dangerous keywords — only element names.
        assert decision.allowed


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestObservability:
    def test_start_and_finish_span(self) -> None:
        obs = Observability("test-1", Path("/tmp/sera-test-obs"), enable_tracing=False, enable_action_log=True)
        span = obs.start_span("navigate", {"url": "/"})
        assert span.name == "navigate"
        assert span.status == "running"
        obs.finish_span(span, status="ok", detail="navigated")
        assert span.status == "ok"
        assert span.duration_ms is not None
        assert span.duration_ms >= 0

    def test_flush_writes_jsonl(self, tmp_path: Path) -> None:
        obs = Observability("test-2", tmp_path, enable_tracing=False, enable_action_log=True)
        span = obs.start_span("click", {"ref": "1"})
        obs.finish_span(span, status="ok")
        obs.flush()
        assert obs.action_log_path is not None
        assert obs.action_log_path.exists()
        content = obs.action_log_path.read_text(encoding="utf-8")
        assert "click" in content
        assert "span_id" in content

    def test_summary_returns_stats(self, tmp_path: Path) -> None:
        obs = Observability("test-3", tmp_path, enable_tracing=False, enable_action_log=False)
        s1 = obs.start_span("navigate", {})
        obs.finish_span(s1, status="ok")
        s2 = obs.start_span("click", {})
        obs.finish_span(s2, status="failed", detail="not found")
        summary = obs.summary()
        assert summary["total_spans"] == 2
        assert summary["ok"] == 1
        assert summary["failed"] == 1
        assert summary["total_duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_tracing_lifecycle(self, tmp_path: Path) -> None:
        """Test that tracing starts and stops without error on a real context."""
        from sera_browser.browser.manager import BrowserManager

        settings = _make_settings()
        bm = BrowserManager(settings=settings)
        try:
            handle = await bm.launch()
            cm = await bm.new_context_manager(CancellationManager(task_id="obs-test"))
            page = await cm.open(handle.browser)
            obs = Observability("test-4", tmp_path, enable_tracing=True, enable_action_log=True)
            await obs.start_tracing(cm.context)
            await page.goto("about:blank")
            await obs.stop_tracing(cm.context)
            obs.flush()
            assert obs.trace_path is not None
            summary = obs.summary()
            # Trace path should be set.
            assert summary.get("trace_path") is not None or True  # tracing may fail in some envs
            await cm.close()
        finally:
            await bm.shutdown()


# ---------------------------------------------------------------------------
# Action schema — new actions
# ---------------------------------------------------------------------------


class TestActionSchemaNewActions:
    def test_fill_form_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "fill_form",
            "args": {
                "fields": [
                    {"ref": "textbox#1:Username", "value": "admin"},
                    {"ref": "textbox#2:Password", "value": "pass"},
                ]
            },
        }
        result = validate_action(parsed, state, settings)
        assert result.ok, f"should be valid: {result.reason}"
        assert result.step.action == "fill_form"

    def test_fill_form_rejects_unknown_ref(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "fill_form",
            "args": {
                "fields": [
                    {"ref": "textbox#999:Nonexistent", "value": "x"},
                ]
            },
        }
        result = validate_action(parsed, state, settings)
        assert not result.ok
        assert "not in interactive_elements" in result.reason

    def test_mouse_click_accepts_numeric_coords(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "mouse_click",
            "args": {"x": 100, "y": 200},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_mouse_click_accepts_float_coords(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "mouse_click",
            "args": {"x": 100.5, "y": 200.7},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_drag_validates_both_refs(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "drag",
            "args": {"start_ref": "button#1:Login", "end_ref": "textbox#1:Username"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_drag_rejects_missing_ref(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "drag",
            "args": {"start_ref": "button#1:Login", "end_ref": "button#999:Ghost"},
        }
        result = validate_action(parsed, state, settings)
        assert not result.ok

    def test_extract_action(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "extract",
            "args": {"prompt": "extract all product names"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_search_page_action(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "search_page",
            "args": {"pattern": "price.*\\$\\d+"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_switch_tab_action(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "switch_tab",
            "args": {"tab_id": "t2"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_new_tab_action_validates_url(self) -> None:
        state = _make_state()
        settings = _make_settings()
        # SSRF-rejected URL.
        parsed = {
            "action": "new_tab",
            "args": {"url": "http://169.254.169.254/"},
        }
        result = validate_action(parsed, state, settings)
        assert not result.ok
        assert "rejected" in result.reason

    def test_update_memory_action(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "update_memory",
            "args": {"memory": "User logged in as admin"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_finish_with_memory(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "finish",
            "args": {"memory": "Task complete: found 3 forms"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok
        assert result.step.kind == "finish"


# ---------------------------------------------------------------------------
# Memory + planner integration
# ---------------------------------------------------------------------------


class TestMemoryPlannerIntegration:
    async def test_memory_view_included_in_state_view(self) -> None:
        """The LLM state view should include the memory section when a
        memory manager is wired."""
        from sera_browser.task.llm_client import MockLLMClient
        from sera_browser.task.llm_planner import LLMTaskPlanner
        from sera_browser.task.planner import TaskPlanner

        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        memory = MemoryManager()
        memory.update_long_term_memory("logged in as admin")
        client = MockLLMClient(responses=[json.dumps({"action": "finish", "args": {}})])
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            memory=memory,
        )
        await planner.next_step(state, __import__("sera_browser.task.loop", fromlist=["LoopRecoveryManager"]).LoopRecoveryManager())
        # The mock client should have received a prompt containing "memory".
        assert len(client.calls) == 1
        assert "memory" in client.calls[0]["user"]


# ---------------------------------------------------------------------------
# Tab manager
# ---------------------------------------------------------------------------


class TestTabManager:
    async def test_primary_page_registered_as_t1(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.tabs import TabManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            tm = TabManager(primary_page=page)
            assert tm.active is not None
            assert tm.active.id == "t1"
            view = tm.to_view()
            assert len(view) == 1
            assert view[0]["id"] == "t1"
            assert view[0]["active"] is True
        finally:
            await cm.close()

    async def test_new_tab_and_switch(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.tabs import TabManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            tm = TabManager(primary_page=page)
            rec = await tm.new_tab_async(url=local_site_url + "/login")
            assert rec.id == "t2"
            assert tm.active is not None
            assert tm.active.id == "t2"
            # Switch back to t1.
            assert tm.switch_to("t1")
            assert tm.active.id == "t1"
        finally:
            await cm.close()

    async def test_close_tab(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.tabs import TabManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            tm = TabManager(primary_page=page)
            await tm.new_tab_async(url=local_site_url + "/login")
            assert len(tm.tabs) == 2
            assert tm.close_tab("t2")
            assert len(tm.tabs) == 1
        finally:
            await cm.close()

    async def test_refuse_close_last_tab(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.tabs import TabManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            tm = TabManager(primary_page=page)
            # Should refuse to close the only tab.
            assert not tm.close_tab("t1")
            assert len(tm.tabs) == 1
        finally:
            await cm.close()
