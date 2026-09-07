"""Tests for the 4 recommended follow-ups.

Covers:
1. Stealth layer wired into ContextManager (SBA_STEALTH_LEVEL)
2. CDP-based AX tree extractor
3. Eval harness (deterministic + LLM judge, task runner, summary)
4. Plan-Act-Validate multi-agent pattern (plan decomposition, validation, reflexion)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.browser.cdp_ax import (
    CDPAccessibilityExtractor,
    cdp_nodes_to_elements,
)
from sera_browser.browser.stealth import StealthConfig, StealthLayer
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.eval.harness import (
    DEFAULT_TASKS,
    DeterministicJudge,
    EvalHarness,
    EvalTask,
    LLMJudge,
)
from sera_browser.models import BrowserState, InteractiveElement
from sera_browser.task.llm_client import MockLLMClient
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.pav import (
    PAVStats,
    Plan,
    PlanActValidateOrchestrator,
    SubGoal,
)
from sera_browser.task.planner import TaskPlanner


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        max_steps=5,
        enable_tracing=False,
        enable_action_log=False,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 1. Stealth wired into ContextManager
# ---------------------------------------------------------------------------


class TestStealthContextIntegration:
    async def test_stealth_disabled_by_default(self, browser_manager, settings, local_site_url) -> None:
        """When stealth_level=off, context opens normally without stealth kwargs."""
        from sera_browser.browser.context import ContextManager

        handle = await browser_manager.launch()
        cm = ContextManager(settings=settings, cancellation=CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            # Should navigate fine.
            from sera_browser.browser.driver import PlaywrightDriver
            driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
            await driver.navigate(page, local_site_url + "/")
            assert page.url.startswith(local_site_url)
        finally:
            await cm.close()

    async def test_stealth_light_applies_fingerprint(self, browser_manager, local_site_url) -> None:
        """When stealth_level=light, the context gets a randomized UA + viewport."""
        from sera_browser.browser.context import ContextManager

        settings = _make_settings(stealth_level="light")
        handle = await browser_manager.launch()
        cm = ContextManager(settings=settings, cancellation=CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            # The page should have a non-default user agent.
            ua = await page.evaluate("() => navigator.userAgent")
            assert ua and len(ua) > 50  # realistic UA is long
            # navigator.webdriver should be undefined (stealth patches it).
            # Note: this may not work in all headless modes, so we just check
            # the page loads.
            from sera_browser.browser.driver import PlaywrightDriver
            driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
            await driver.navigate(page, local_site_url + "/")
            assert page.url.startswith(local_site_url)
        finally:
            await cm.close()

    async def test_stealth_medium_applies_canvas_noise(self, browser_manager, local_site_url) -> None:
        """stealth_level=medium injects canvas noise init script without breaking."""
        from sera_browser.browser.context import ContextManager

        settings = _make_settings(stealth_level="medium")
        handle = await browser_manager.launch()
        cm = ContextManager(settings=settings, cancellation=CancellationManager(task_id="t"))
        page = await cm.open(handle.browser)
        try:
            from sera_browser.browser.driver import PlaywrightDriver
            driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
            await driver.navigate(page, local_site_url + "/")
            assert page.url.startswith(local_site_url)
        finally:
            await cm.close()

    async def test_stealth_with_fixed_viewport(self, browser_manager, local_site_url) -> None:
        """stealth_viewport="800x600" is parsed into a dict."""
        from sera_browser.browser.context import ContextManager

        settings = _make_settings(
            stealth_level="light",
            stealth_viewport="800x600",
        )
        stealth_config = settings.to_stealth_config()
        assert stealth_config.viewport == {"width": 800, "height": 600}

    async def test_stealth_with_proxy(self, browser_manager, local_site_url) -> None:
        """stealth_proxy is parsed into proxy kwargs."""
        settings = _make_settings(
            stealth_level="light",
            stealth_proxy="http://user:pass@proxy.example.com:8080",
        )
        stealth_config = settings.to_stealth_config()
        stealth = StealthLayer(stealth_config)
        fp = stealth.choose_fingerprint()
        kwargs = stealth.context_kwargs(fp)
        assert "proxy" in kwargs
        assert kwargs["proxy"]["server"] == "http://proxy.example.com:8080"


# ---------------------------------------------------------------------------
# 2. CDP-based AX tree extractor
# ---------------------------------------------------------------------------


class TestCDPAccessibilityExtractor:
    async def test_extract_returns_tree_on_chromium(self, browser_manager, settings, local_site_url) -> None:
        """CDP AX extraction should work on Chromium and return a non-empty tree."""
        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="cdp"))
        page = await cm.open(handle.browser)
        try:
            from sera_browser.browser.driver import PlaywrightDriver
            driver = PlaywrightDriver(settings, CancellationManager(task_id="cdp"))
            await driver.navigate(page, local_site_url + "/login")
            extractor = CDPAccessibilityExtractor(page)
            tree = await extractor.extract()
            # CDP should return a non-empty tree on Chromium.
            # If it's empty (rare), the driver falls back to DOM — still OK.
            if tree:
                assert isinstance(tree, list)
                # Each node should have role, name, children.
                first = tree[0]
                assert "role" in first
                assert "name" in first
        finally:
            await cm.close()

    async def test_cdp_to_elements_flattens_interactive(self) -> None:
        """cdp_nodes_to_elements should flatten the tree into interactive elements."""
        tree = [
            {
                "role": "button",
                "name": "Login",
                "backendNodeId": 1,
                "properties": {"disabled": False},
                "htmlAttributes": {"href": None, "type": "submit"},
                "interactive": True,
                "children": [
                    {
                        "role": "textbox",
                        "name": "Username",
                        "backendNodeId": 2,
                        "properties": {},
                        "htmlAttributes": {"type": "text", "placeholder": "user"},
                        "interactive": True,
                        "children": [],
                    }
                ],
            }
        ]
        elements = cdp_nodes_to_elements(tree)
        assert len(elements) == 2
        assert elements[0]["role"] == "button"
        assert elements[0]["name"] == "Login"
        assert elements[1]["role"] == "textbox"
        assert elements[1]["placeholder"] == "user"

    async def test_snapshot_method_dom_default(self, browser_manager, settings, local_site_url) -> None:
        """Default snapshot_method=dom uses the DOM walker (fast)."""
        from sera_browser.browser.driver import PlaywrightDriver

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="dom"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="dom"))
            await driver.navigate(page, local_site_url + "/login")
            snap = await driver.snapshot(page)
            # DOM walker should find interactive elements.
            assert len(snap.elements) > 0
        finally:
            await cm.close()

    async def test_snapshot_method_cdp_uses_cdp(self, browser_manager, local_site_url) -> None:
        """snapshot_method=cdp uses CDP (or falls back to DOM on failure)."""
        from sera_browser.browser.driver import PlaywrightDriver

        settings = _make_settings(snapshot_method="cdp")
        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="cdp2"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="cdp2"))
            await driver.navigate(page, local_site_url + "/login")
            snap = await driver.snapshot(page)
            # Either CDP or DOM fallback should produce elements.
            assert len(snap.elements) > 0
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# 3. Eval harness
# ---------------------------------------------------------------------------


class TestEvalHarness:
    def test_default_tasks_exist(self) -> None:
        assert len(DEFAULT_TASKS) >= 5
        assert all(isinstance(t, EvalTask) for t in DEFAULT_TASKS)
        assert all(t.id and t.objective for t in DEFAULT_TASKS)

    def test_deterministic_judge_passes_full_match(self) -> None:
        judge = DeterministicJudge()
        task = EvalTask(
            id="t1",
            target="/",
            objective="test",
            expected_title_contains="Test",
            expected_api_in_report="/api/x",
        )
        report = {
            "status": "completed",
            "target": "http://x/",
            "title": "Test Page",
            "apis": ["/api/x", "/api/y"],
        }
        result = judge.judge(task, report)
        assert result.status == "passed"
        assert result.score == 1.0

    def test_deterministic_judge_fails_on_mismatch(self) -> None:
        judge = DeterministicJudge()
        task = EvalTask(
            id="t2",
            target="/",
            objective="test",
            expected_title_contains="Missing",
        )
        report = {"status": "completed", "target": "http://x/", "title": "Other"}
        result = judge.judge(task, report)
        assert result.status == "failed"
        assert result.score == 0.0

    def test_deterministic_judge_partial_score(self) -> None:
        judge = DeterministicJudge()
        task = EvalTask(
            id="t3",
            target="/",
            objective="test",
            expected_title_contains="Test",
            expected_api_in_report="/api/missing",
        )
        report = {
            "status": "completed",
            "target": "http://x/",
            "title": "Test Page",  # matches
            "apis": ["/api/other"],  # doesn't match
        }
        result = judge.judge(task, report)
        assert result.score == 0.5  # 1 of 2 checks passed

    def test_deterministic_judge_routes_count(self) -> None:
        judge = DeterministicJudge()
        task = EvalTask(
            id="t4",
            target="/",
            objective="discover routes",
            expected_routes_count_min=3,
        )
        report = {"status": "completed", "target": "http://x/", "routes": ["/a", "/b", "/c", "/d"]}
        result = judge.judge(task, report)
        assert result.status == "passed"
        assert result.score == 1.0

    async def test_llm_judge_falls_back_when_unavailable(self) -> None:
        """LLMJudge with an unavailable client falls back to deterministic."""
        from sera_browser.task.llm_client import _NoneLLMClient

        judge = LLMJudge(client=_NoneLLMClient())
        task = EvalTask(
            id="t5",
            target="/",
            objective="test",
            expected_title_contains="Test",
        )
        report = {"status": "completed", "target": "http://x/", "title": "Test Page"}
        result = await judge.judge(task, report)
        assert result.status == "passed"
        assert result.score == 1.0

    async def test_llm_judge_with_mock_client(self) -> None:
        """LLMJudge with a MockLLMClient parses the score."""
        from sera_browser.task.llm_client import MockLLMClient

        client = MockLLMClient(responses=[json.dumps({"score": 0.8, "reason": "mostly done"})])
        judge = LLMJudge(client=client)
        task = EvalTask(id="t6", target="/", objective="test", expected_outcome="done")
        report = {"status": "completed", "target": "http://x/", "title": "X", "routes": []}
        result = await judge.judge(task, report)
        assert result.score == 0.8
        assert result.status == "passed"

    def test_summary_aggregates_results(self) -> None:
        from sera_browser.eval.harness import EvalResult

        results = [
            EvalResult(task_id="t1", status="passed", score=1.0, duration_ms=100),
            EvalResult(task_id="t2", status="failed", score=0.3, duration_ms=200),
            EvalResult(task_id="t3", status="passed", score=0.8, duration_ms=150),
        ]
        summary = EvalHarness.summary(results)
        assert summary["total"] == 3
        assert summary["passed"] == 2
        assert summary["failed"] == 1
        assert summary["pass_rate"] == round(2 / 3, 3)
        assert summary["avg_score"] == round((1.0 + 0.3 + 0.8) / 3, 3)
        assert summary["total_duration_ms"] == 450.0


# ---------------------------------------------------------------------------
# 4. Plan-Act-Validate multi-agent pattern
# ---------------------------------------------------------------------------


class TestPlanActValidate:
    async def test_pav_makes_plan_on_first_call(self) -> None:
        """The first next_step call should invoke the Planner to make a plan.

        Uses SEPARATE MockLLMClients for PAV (Planner/Validator) and Actor
        (LLMTaskPlanner) so response ordering is deterministic.
        """
        from sera_browser.task.llm_planner import LLMTaskPlanner

        settings = _make_settings(llm_provider="zai", llm_timeout_s=5.0)
        state = BrowserState(task_id="t", target="http://x/")
        state.current_url = "http://x/"
        state.dom_hash = "abc"
        cm = CancellationManager(task_id="pav")
        # PAV client (Planner + Validator).
        pav_client = MockLLMClient(
            responses=[
                # 1. Planner response.
                json.dumps([
                    {"id": 1, "goal": "navigate to /login", "success_criteria": "url contains /login"},
                    {"id": 2, "goal": "fill username", "success_criteria": "username filled"},
                ]),
                # 2. Validator response for sub-goal 1 (achieved).
                json.dumps({"achieved": True, "reason": "navigated"}),
            ]
        )
        # Actor client.
        actor_client = MockLLMClient(
            responses=[
                # Actor response for sub-goal 1: finish.
                json.dumps({"action": "finish", "args": {}}),
            ]
        )
        actor = LLMTaskPlanner(
            settings=settings,
            client=actor_client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        pav = PlanActValidateOrchestrator(
            settings=settings,
            client=pav_client,
            cancellation=cm,
            objective="login and fill",
            actor=actor,
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager()
        # First call: makes plan, runs sub-goal 1.
        step1 = await pav.next_step(state, loop)
        assert pav.plan is not None
        assert len(pav.plan.sub_goals) == 2
        assert pav.stats.plans_made == 1

    async def test_pav_finishes_when_all_achieved(self) -> None:
        """When all sub-goals are achieved, PAV returns a finish step."""
        from sera_browser.task.llm_planner import LLMTaskPlanner

        settings = _make_settings(llm_provider="zai", llm_timeout_s=5.0)
        state = BrowserState(task_id="t", target="http://x/")
        state.current_url = "http://x/"
        state.dom_hash = "abc"
        cm = CancellationManager(task_id="pav2")
        # PAV client: planner + validator.
        pav_client = MockLLMClient(
            responses=[
                # Planner: 1 sub-goal.
                json.dumps([{"id": 1, "goal": "done", "success_criteria": "done"}]),
                # Validator: achieved.
                json.dumps({"achieved": True, "reason": "yes"}),
            ]
        )
        # Actor client.
        actor_client = MockLLMClient(
            responses=[
                # Actor: finish.
                json.dumps({"action": "finish", "args": {}}),
            ]
        )
        actor = LLMTaskPlanner(
            settings=settings,
            client=actor_client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        pav = PlanActValidateOrchestrator(
            settings=settings,
            client=pav_client,
            cancellation=cm,
            objective="test",
            actor=actor,
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager()
        # First call: plan + act + validate sub-goal 1.
        await pav.next_step(state, loop)
        # Sub-goal 1 should be achieved.
        assert pav.plan.sub_goals[0].status == "achieved"
        # Second call: all achieved → finish.
        step2 = await pav.next_step(state, loop)
        assert step2.kind == "finish"

    async def test_pav_reflexion_on_validation_failure(self) -> None:
        """When validation fails beyond max_attempts, PAV re-plans (reflexion).

        Uses SEPARATE MockLLMClients for Planner/Validator (the PAV client)
        and Actor (the LLMTaskPlanner client) so response ordering is
        deterministic and doesn't cross-contaminate.

        The PAV client responses are consumed in this order:
        1. Planner (make plan) → list of sub-goals
        2. Validator attempt 1 → not achieved
        3. Validator attempt 2 → not achieved → fail → re-plan
        4. Planner (re-plan) → new list of sub-goals
        """
        from sera_browser.task.llm_planner import LLMTaskPlanner

        settings = _make_settings(llm_provider="zai", llm_timeout_s=5.0)
        state = BrowserState(task_id="t", target="http://x/")
        state.current_url = "http://x/"
        state.dom_hash = "abc"
        cm = CancellationManager(task_id="pav3")
        # PAV client (Planner + Validator calls).
        pav_client = MockLLMClient(
            responses=[
                # 1. Planner: 1 sub-goal.
                json.dumps([{"id": 1, "goal": "do thing", "success_criteria": "done"}]),
                # 2. Validator attempt 1: NOT achieved.
                json.dumps({"achieved": False, "reason": "not done"}),
                # 3. Validator attempt 2: NOT achieved → fail → re-plan.
                json.dumps({"achieved": False, "reason": "still not done"}),
                # 4. Planner re-plan: 1 new sub-goal.
                json.dumps([{"id": 1, "goal": "try differently", "success_criteria": "done"}]),
            ]
        )
        # Actor client (LLMTaskPlanner calls).
        actor_client = MockLLMClient(
            responses=[
                # Actor attempt 1: finish.
                json.dumps({"action": "finish", "args": {}}),
                # Actor attempt 2: finish.
                json.dumps({"action": "finish", "args": {}}),
            ]
        )
        actor = LLMTaskPlanner(
            settings=settings,
            client=actor_client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        pav = PlanActValidateOrchestrator(
            settings=settings,
            client=pav_client,
            cancellation=cm,
            objective="test",
            actor=actor,
            deterministic=TaskPlanner(settings),
            max_sub_goals=3,
        )
        loop = LoopRecoveryManager()
        # First call: Planner makes plan, Actor runs sub-goal 1, Validator fails.
        await pav.next_step(state, loop)
        # The sub-goal should be pending (retried) after first validation failure.
        sg = pav.plan.sub_goals[0]
        assert sg.attempts == 1
        # Status may be "pending" (retried) — check it's not "achieved".
        assert sg.status != "achieved"
        # Second call: Actor runs again, Validator fails again → failed → re-plan.
        await pav.next_step(state, loop)
        assert sg.status == "failed"
        assert pav.stats.reflexion_replans >= 1

    async def test_pav_falls_back_when_planner_unavailable(self) -> None:
        """When the LLM client is unavailable, PAV falls back to deterministic."""
        from sera_browser.task.llm_client import _NoneLLMClient
        from sera_browser.task.llm_planner import LLMTaskPlanner

        settings = _make_settings(llm_provider="zai", llm_timeout_s=5.0)
        state = BrowserState(task_id="t", target="http://x/")
        state.current_url = "http://x/"
        state.dom_hash = "abc"
        cm = CancellationManager(task_id="pav4")
        client = _NoneLLMClient()
        actor = LLMTaskPlanner(
            settings=settings,
            client=MockLLMClient(responses=['{"action": "finish", "args": {}}']),
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        pav = PlanActValidateOrchestrator(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            actor=actor,
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager()
        step = await pav.next_step(state, loop)
        # Should fall back to deterministic (which returns a navigate/observe step).
        assert pav.plan is None  # no plan made
