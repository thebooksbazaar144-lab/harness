"""Tests for the LLM-backed TaskPlanner.

Covers:
- valid LLM action (parsed + validated + executed via ActionExecutor)
- invalid action (unknown action / missing ref / bad args)
- LLM timeout → deterministic fallback
- LLM unavailable → deterministic fallback
- LLM cancelled mid-call → TaskCancelledError propagates
- loop recovery still works under the LLM planner
- cancellation during LLM planning
- action schema validator unit tests
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.cancel import CancellationManager, TaskCancelledError
from sera_browser.config import Settings
from sera_browser.models import BrowserState, InteractiveElement
from sera_browser.task.action_schema import (
    ActionValidationError,
    parse_llm_response,
    validate_action,
)
from sera_browser.task.llm_client import (
    LLMTimeoutError,
    LLMUnavailableError,
    MockLLMClient,
)
from sera_browser.task.llm_planner import LLMTaskPlanner
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.planner import TaskPlanner


# Action schema validator tests are sync — no asyncio mark needed.
# LLM planner tests are async and use the module-level pytestmark below.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(
    *,
    target: str = "http://127.0.0.1:9999/",
    current_url: str | None = "http://127.0.0.1:9999/",
    elements: list[InteractiveElement] | None = None,
    dom_hash: str | None = "abc123",
    technologies: list[str] | None = None,
) -> BrowserState:
    state = BrowserState(task_id="test", target=target)
    state.current_url = current_url
    state.dom_hash = dom_hash
    state.technologies = technologies or []
    if elements is not None:
        state.interactive_elements = elements
    else:
        state.interactive_elements = [
            InteractiveElement(
                ref="button#1:Login",
                role="button",
                name="Login",
                tag="button",
            ),
            InteractiveElement(
                ref="textbox#1:Username",
                role="textbox",
                name="Username",
                tag="input",
                type="text",
            ),
            InteractiveElement(
                ref="link#1:Products",
                role="link",
                name="Products",
                tag="a",
                href="/products",
            ),
        ]
    return state


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost,127.0.0.1:9999",
        max_steps=10,
        llm_provider="zai",
        llm_timeout_s=5.0,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Action schema validator unit tests
# ---------------------------------------------------------------------------


class TestActionSchema:
    def test_parse_plain_json(self) -> None:
        raw = '{"thinking": "x", "action": "finish", "args": {}, "reason": "done"}'
        parsed = parse_llm_response(raw)
        assert parsed["action"] == "finish"

    def test_parse_markdown_fenced_json(self) -> None:
        raw = '```json\n{"action": "finish", "args": {}}\n```'
        parsed = parse_llm_response(raw)
        assert parsed["action"] == "finish"

    def test_parse_json_with_leading_prose(self) -> None:
        raw = 'Here is my decision:\n{"action": "click", "args": {"ref": "button#1:Login"}}'
        parsed = parse_llm_response(raw)
        assert parsed["action"] == "click"
        assert parsed["args"]["ref"] == "button#1:Login"

    def test_parse_empty_raises(self) -> None:
        with pytest.raises(ActionValidationError):
            parse_llm_response("")
        with pytest.raises(ActionValidationError):
            parse_llm_response("   ")

    def test_parse_non_json_raises(self) -> None:
        with pytest.raises(ActionValidationError):
            parse_llm_response("this is not json at all")

    def test_validate_unknown_action_rejected(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "execute_shell", "args": {"cmd": "rm -rf /"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is False
        assert "unknown action" in result.reason

    def test_validate_valid_click(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "click", "args": {"ref": "button#1:Login"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is True
        assert result.step is not None
        assert result.step.action == "click"
        assert result.step.args["ref"] == "button#1:Login"

    def test_validate_click_with_unknown_ref_rejected(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "click", "args": {"ref": "button#999:DoesNotExist"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is False
        assert "not present" in result.reason

    def test_validate_fill_missing_value_rejected(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "fill", "args": {"ref": "textbox#1:Username"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is False
        assert "missing required arg" in result.reason

    def test_validate_navigate_ssrf_rejected(self) -> None:
        state = _make_state()
        settings = _make_settings()
        # Cloud-metadata endpoint — always blocked.
        parsed = {"action": "navigate", "args": {"url": "http://169.254.169.254/"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is False
        assert "rejected" in result.reason

    def test_validate_navigate_allowed(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "navigate", "args": {"url": "http://127.0.0.1:9999/login"}}
        result = validate_action(parsed, state, settings)
        assert result.ok is True
        assert result.step.action == "navigate"

    def test_validate_extra_args_rejected(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "click",
            "args": {"ref": "button#1:Login", "javascript": "alert(1)"},
        }
        result = validate_action(parsed, state, settings)
        assert result.ok is False
        assert "unexpected args" in result.reason

    def test_validate_finish_accepted(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "finish", "args": {}}
        result = validate_action(parsed, state, settings)
        assert result.ok is True
        assert result.step.kind == "finish"

    def test_validate_bool_as_int_rejected(self) -> None:
        # Defense in depth: bools must not masquerade as ints.
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "scroll", "args": {"dy": True}}
        result = validate_action(parsed, state, settings)
        assert result.ok is False


# ---------------------------------------------------------------------------
# LLM planner behavior tests
# ---------------------------------------------------------------------------


class TestLLMTaskPlanner:
    async def test_valid_llm_action_returns_step(self) -> None:
        """The LLM returns a valid click action; planner returns the Step."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[
                json.dumps(
                    {
                        "thinking": "click the login button",
                        "action": "click",
                        "args": {"ref": "button#1:Login"},
                        "reason": "user asked to log in",
                    }
                )
            ]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="click the login button",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step.action == "click"
        assert step.args["ref"] == "button#1:Login"
        assert planner.stats.llm_succeeded == 1
        assert planner.stats.fallback_to_deterministic == 0

    async def test_invalid_action_falls_back_to_deterministic(self) -> None:
        """LLM returns an unknown action; planner falls back."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[
                json.dumps({"action": "execute_shell", "args": {"cmd": "ls"}}),
            ]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        # Should have fallen back to deterministic planner.
        assert planner.stats.invalid_action == 1
        assert planner.stats.fallback_to_deterministic == 1
        # The deterministic planner's first step (after initial navigate) is
        # a noop observation step — not the LLM's "execute_shell".
        assert step.action != "execute_shell"

    async def test_invalid_ref_falls_back_to_deterministic(self) -> None:
        """LLM returns click with a ref that doesn't exist; fallback."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[
                json.dumps(
                    {
                        "action": "click",
                        "args": {"ref": "button#999:Ghost"},
                    }
                )
            ]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert planner.stats.invalid_action == 1
        assert planner.stats.fallback_to_deterministic == 1
        assert step.action != "click"

    async def test_unparseable_response_falls_back(self) -> None:
        """LLM returns non-JSON; planner falls back."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(responses=["this is not json"])
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert planner.stats.invalid_action == 1
        assert planner.stats.fallback_to_deterministic == 1

    async def test_timeout_falls_back_to_deterministic(self) -> None:
        """LLM call times out; planner falls back."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(responses=[LLMTimeoutError("simulated timeout")])
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            timeout_s=0.5,
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert planner.stats.llm_timeout == 1
        assert planner.stats.fallback_to_deterministic == 1

    async def test_llm_unavailable_falls_back(self) -> None:
        """LLM client raises LLMUnavailableError; planner falls back."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(responses=[LLMUnavailableError("CLI missing")])
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert planner.stats.llm_unavailable == 1
        assert planner.stats.fallback_to_deterministic == 1

    async def test_loop_recovery_redirects_llm_instead_of_blocking(self) -> None:
        """When tool repetition fires, the planner re-prompts the LLM with an
        anti-repetition directive instead of declaring the task blocked."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        # The LLM now returns a DIFFERENT action (navigate) — the redirect
        # should let this through instead of blocking the task.
        client = MockLLMClient(
            responses=[json.dumps({"action": "navigate", "args": {"url": state.target}})]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
        # Simulate tool repetition: same call 3 times.
        loop.record_tool("click", "button#1:Login")
        loop.record_tool("click", "button#1:Login")
        loop.record_tool("click", "button#1:Login")
        step = await planner.next_step(state, loop)
        # The redirect path called the LLM once with the directive.
        assert planner.stats.repetition_redirects == 1
        assert planner.stats.llm_calls == 1
        assert len(client.calls) == 1
        # The directive was injected into the prompt.
        assert "FORBIDDEN" in client.calls[0]["user"]
        assert "click(button#1:Login)" in client.calls[0]["user"]
        # The tool history was reset (fresh repetition budget).
        assert loop.tool_loop() is None
        # The LLM's different action was accepted.
        assert step.kind == "navigate"
        assert step.action == "navigate"

    async def test_loop_redirect_forbidden_action_goes_deterministic(self) -> None:
        """If the LLM ignores the directive and repeats the forbidden action,
        the planner falls back to the deterministic planner (no execution)."""
        state = _make_state()
        state.current_url = state.target  # past initial navigation
        state.dom_hash = "abc123"
        state.technologies = ["jQuery"]
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        # LLM returns the SAME action that caused the loop.
        client = MockLLMClient(
            responses=[json.dumps({"action": "click", "args": {"ref": "button#1:Login"}})]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
        for _ in range(3):
            loop.record_tool("click", "button#1:Login")
        step = await planner.next_step(state, loop)
        # The forbidden guard fired: deterministic planner took over.
        assert planner.stats.repetition_redirects == 1
        assert planner.stats.invalid_action == 1
        assert planner.stats.fallback_to_deterministic == 1
        # v4: the deterministic coverage planner abandons the stuck target —
        # it must NOT re-issue the same click on the SAME (blocked) ref.
        if step.action == "click":
            assert step.args.get("ref") != "button#1:Login"

    async def test_loop_redirect_exhaustion_blocks(self) -> None:
        """After the bounded number of redirects, further repetition blocks."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[json.dumps({"action": "finish", "args": {}})]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        # Exhaust all redirects.
        planner._max_repetition_redirects = 0
        loop = LoopRecoveryManager(tool_repeat_limit=3, semantic_loop_limit=3)
        for _ in range(3):
            loop.record_tool("click", "button#1:Login")
        step = await planner.next_step(state, loop)
        assert step.kind == "blocked"
        assert planner.stats.llm_calls == 0

    async def test_cancellation_during_llm_call_propagates(self) -> None:
        """If cancellation fires during the LLM call, TaskCancelledError
        propagates so the orchestrator can clean up."""
        state = _make_state()
        # Pre-cancel so the client's `complete()` raises immediately.
        cm = CancellationManager(task_id="t")
        cm.cancel(reason="test-cancel")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[json.dumps({"action": "finish", "args": {}})]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        with pytest.raises(TaskCancelledError):
            await planner.next_step(state, LoopRecoveryManager())
        assert planner.stats.llm_cancelled == 1

    async def test_structured_state_view_does_not_leak_internals(self) -> None:
        """The state view sent to the LLM must NOT contain Playwright objects,
        raw cookies, storage_state, or credential values."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        captured: dict[str, Any] = {}

        def capture_prompt(system: str, user: str) -> str:
            captured["system"] = system
            captured["user"] = user
            return json.dumps({"action": "finish", "args": {}})

        client = MockLLMClient(responses=[capture_prompt])
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        await planner.next_step(state, LoopRecoveryManager())
        user = captured["user"]
        # Must contain structured state...
        assert "CURRENT BROWSER STATE" in user
        assert "interactive_elements" in user
        # ...but NOT raw Playwright internals or credential-bearing fields.
        assert "storage_state" not in user.lower()
        # The state view is the JSON blob between "CURRENT BROWSER STATE"
        # and the next blank line. Parse it to verify structure.
        marker = "CURRENT BROWSER STATE (JSON):\n"
        idx = user.find(marker)
        assert idx != -1, "state view marker not found in prompt"
        json_start = user.find("{", idx + len(marker))
        # The JSON blob ends at the first blank line after it starts.
        blob_end = user.find("\n\n", json_start)
        if blob_end == -1:
            blob_end = len(user)
        blob = user[json_start:blob_end]
        parsed = json.loads(blob)
        assert parsed["target"] == state.target
        assert isinstance(parsed["interactive_elements"], list)
        # Verify NO credential-bearing keys are present in the view.
        def _walk(obj: Any) -> list[str]:
            keys: list[str] = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    keys.append(k.lower())
                    keys.extend(_walk(v))
            elif isinstance(obj, list):
                for item in obj:
                    keys.extend(_walk(item))
            return keys
        all_keys = _walk(parsed)
        assert "password" not in all_keys or "has_password" in all_keys
        assert "cookie" not in all_keys
        assert "authorization" not in all_keys
        assert "token" not in all_keys

    async def test_finish_action_terminates_loop(self) -> None:
        """When the LLM returns `finish`, the Step's kind is 'finish' so the
        orchestrator stops the loop."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[json.dumps({"action": "finish", "args": {}, "reason": "done"})]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step.kind == "finish"
        assert step.action == "finish"

    async def test_navigate_to_same_url_is_idempotent_at_executor(self) -> None:
        """LLM chooses navigate to the current URL; the validator accepts it
        (the executor will treat it as a no-op snapshot refresh)."""
        state = _make_state(current_url="http://127.0.0.1:9999/login")
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[
                json.dumps(
                    {
                        "action": "navigate",
                        "args": {"url": "http://127.0.0.1:9999/login"},
                    }
                )
            ]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step.action == "navigate"
        assert step.args["url"] == "http://127.0.0.1:9999/login"
        assert planner.stats.llm_succeeded == 1

    async def test_consecutive_fallbacks_are_counted(self) -> None:
        """Multiple invalid actions in a row increment the fallback counter
        each time, so the orchestrator can detect a degraded LLM."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            responses=[
                json.dumps({"action": "bogus", "args": {}}),
                json.dumps({"action": "alsobogus", "args": {}}),
                json.dumps({"action": "stillbogus", "args": {}}),
            ]
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager()
        await planner.next_step(state, loop)
        await planner.next_step(state, loop)
        await planner.next_step(state, loop)
        # The LLM was called 3 times and returned valid JSON each time
        # (so llm_succeeded counts the *call*, not the action validity).
        assert planner.stats.llm_calls == 3
        assert planner.stats.llm_succeeded == 3
        # But each response contained an invalid action → 3 fallbacks.
        assert planner.stats.invalid_action == 3
        assert planner.stats.fallback_to_deterministic == 3
