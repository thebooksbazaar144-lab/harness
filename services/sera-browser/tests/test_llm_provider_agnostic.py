"""Provider-independent LLM layer tests.

Covers:
- `make_llm_client()` factory selects the right implementation
- `LLMConfig.masked_repr()` hides API keys
- `LLMResponse.parsed_json` parses content (with markdown fences + prose)
- Error classification: `LLMAuthError`, `LLMRateLimitError`,
  `LLMUnavailableError`, `LLMTimeoutError`, `LLMResponseError`
- `OpenAICompatLLMClient` HTTP error classification (via httpx mock)
- `MockLLMClient` supports vision toggle
- `LLMTaskPlanner` works identically with any `LLMClient` implementation
  (provider-agnostic — the planner never references `ZaiLLMClient` or
  `OpenAICompatLLMClient` directly)
- Vision escalation is optional and only triggers on loop recovery
- LLM planner stats include `vision_calls` / `vision_succeeded`
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.cancel import CancellationManager, TaskCancelledError
from sera_browser.config import Settings
from sera_browser.models import BrowserState, InteractiveElement
from sera_browser.task.action_schema import parse_llm_response, validate_action
from sera_browser.task.llm_client import (
    LLMAuthError,
    LLMClient,
    LLMConfig,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    MockLLMClient,
    OpenAICompatLLMClient,
    ZaiLLMClient,
    make_llm_client,
)
from sera_browser.task.llm_planner import LLMTaskPlanner
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.planner import TaskPlanner


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestLLMClientFactory:
    def test_factory_returns_none_client_for_provider_none(self) -> None:
        c = make_llm_client(LLMConfig(provider="none"))
        assert c.provider_name == "none"
        assert c.available() is False

    def test_factory_returns_zai_client(self) -> None:
        c = make_llm_client(LLMConfig(provider="zai", model="glm-4-plus"))
        assert isinstance(c, ZaiLLMClient)
        assert c.provider_name == "zai"
        assert c.model == "glm-4-plus"

    def test_factory_returns_openai_client(self) -> None:
        c = make_llm_client(
            LLMConfig(
                provider="openai",
                model="gpt-4o-mini",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
                extra={"vision_capable": True},
            )
        )
        assert isinstance(c, OpenAICompatLLMClient)
        assert c.provider_name == "openai"
        assert c.api_key == "sk-test"
        assert c.base_url == "https://api.openai.com/v1"
        assert c.model == "gpt-4o-mini"
        assert c.supports_vision is True

    def test_factory_unknown_provider_raises(self) -> None:
        with pytest.raises(LLMUnavailableError):
            make_llm_client(LLMConfig(provider="bogus"))  # type: ignore[arg-type]

    def test_factory_openai_without_api_key_is_unavailable(self) -> None:
        c = make_llm_client(LLMConfig(provider="openai", model="gpt-4o-mini"))
        # No api_key → unavailable
        assert c.available() is False

    def test_factory_openai_with_api_key_is_available(self) -> None:
        c = make_llm_client(
            LLMConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")
        )
        assert c.available() is True

    def test_factory_openai_vision_capability_flag(self) -> None:
        c_off = make_llm_client(
            LLMConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")
        )
        assert c_off.supports_vision is False
        c_on = make_llm_client(
            LLMConfig(
                provider="openai",
                model="gpt-4o",
                api_key="sk-test",
                extra={"vision_capable": True},
            )
        )
        assert c_on.supports_vision is True


# ---------------------------------------------------------------------------
# Config masking
# ---------------------------------------------------------------------------


class TestLLMConfigMasking:
    def test_masked_repr_hides_full_api_key(self) -> None:
        cfg = LLMConfig(
            provider="openai",
            api_key="sk-1234567890abcdef",
            model="gpt-4o",
        )
        r = cfg.masked_repr()
        assert "sk-1234567890abcdef" not in r
        assert "sk-1" in r  # first 4 chars visible
        assert "cdef" in r  # last 4 chars visible

    def test_masked_repr_short_key_fully_masked(self) -> None:
        cfg = LLMConfig(provider="openai", api_key="short", model="gpt-4o")
        r = cfg.masked_repr()
        assert "short" not in r
        assert "***" in r

    def test_masked_repr_empty_key(self) -> None:
        cfg = LLMConfig(provider="openai", api_key="", model="gpt-4o")
        r = cfg.masked_repr()
        assert "api_key=''" in r or "api_key='***'" in r


# ---------------------------------------------------------------------------
# LLMResponse.parsed_json
# ---------------------------------------------------------------------------


class TestLLMResponseParsedJson:
    def test_parsed_json_plain(self) -> None:
        r = LLMResponse(content='{"action": "finish", "args": {}}')
        parsed = r.parsed_json
        assert parsed["action"] == "finish"

    def test_parsed_json_markdown_fenced(self) -> None:
        r = LLMResponse(content='```json\n{"action": "click", "args": {"ref": "b1"}}\n```')
        parsed = r.parsed_json
        assert parsed["action"] == "click"

    def test_parsed_json_with_leading_prose(self) -> None:
        r = LLMResponse(content='Here is my decision: {"action": "navigate", "args": {"url": "/x"}}')
        parsed = r.parsed_json
        assert parsed["action"] == "navigate"

    def test_parsed_json_non_json_returns_none(self) -> None:
        r = LLMResponse(content="this is not json")
        assert r.parsed_json is None

    def test_parsed_json_empty_returns_none(self) -> None:
        r = LLMResponse(content="")
        assert r.parsed_json is None

    def test_parsed_json_is_cached(self) -> None:
        r = LLMResponse(content='{"action": "finish"}')
        a = r.parsed_json
        b = r.parsed_json
        assert a is b  # same object reference (cached)


# ---------------------------------------------------------------------------
# MockLLMClient vision support
# ---------------------------------------------------------------------------


class TestMockLLMClientVision:
    def test_default_mock_does_not_support_vision(self) -> None:
        c = MockLLMClient()
        assert c.supports_vision is False

    def test_mock_with_vision_enabled(self) -> None:
        c = MockLLMClient(_supports_vision=True)
        assert c.supports_vision is True

    def test_mock_vision_call_delegates_to_complete(self) -> None:
        c = MockLLMClient(
            responses=[json.dumps({"action": "finish", "args": {}})],
            _supports_vision=True,
        )
        cm = CancellationManager(task_id="t")

        async def _main() -> None:
            r = asyncio.get_event_loop()
            resp = asyncio.run_coroutine_threadsafe(
                c.complete_with_vision(
                    system="s",
                    user="u",
                    image_png=b"\x89PNG fake",
                    timeout_s=5.0,
                    cancellation=cm,
                ),
                r,
            )
            return resp.result(timeout=10)

        # Run it properly via asyncio
        resp = asyncio.run(
            c.complete_with_vision(
                system="s",
                user="u",
                image_png=b"\x89PNG fake",
                timeout_s=5.0,
                cancellation=cm,
            )
        )
        assert resp.content is not None

    def test_mock_without_vision_records_note_in_user_prompt(self) -> None:
        c = MockLLMClient(responses=['{"action": "finish", "args": {}}'])
        cm = CancellationManager(task_id="t")
        asyncio.run(
            c.complete_with_vision(
                system="s",
                user="describe this page",
                image_png=b"\x89PNG fake",
                timeout_s=5.0,
                cancellation=cm,
            )
        )
        assert len(c.calls) == 1
        assert "vision" in c.calls[0]["user"].lower()


# ---------------------------------------------------------------------------
# OpenAICompatLLMClient HTTP error classification
# ---------------------------------------------------------------------------


class TestOpenAICompatErrorClassification:
    """Test that the OpenAI-compatible client maps HTTP errors to the right
    LLM*Error subclass. We use a fake httpx transport to avoid real network."""

    def _make_client_with_fake_transport(
        self, status_code: int, body: bytes = b'{"error":"x"}'
    ) -> OpenAICompatLLMClient:
        import httpx

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, content=body)

        transport = httpx.MockTransport(_handler)
        client = OpenAICompatLLMClient(
            api_key="sk-test",
            base_url="https://fake.test/v1",
            model="gpt-4o-mini",
        )
        # Inject a pre-configured httpx client with our transport.
        client._client = httpx.AsyncClient(
            base_url="https://fake.test/v1",
            headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
            transport=transport,
            timeout=httpx.Timeout(30.0),
        )
        return client

    def test_401_classified_as_auth_error(self) -> None:
        c = self._make_client_with_fake_transport(401, b'{"error":{"message":"bad key"}}')
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMAuthError):
            asyncio.run(
                c.complete(
                    system="s",
                    user="u",
                    timeout_s=5.0,
                    cancellation=cm,
                )
            )

    def test_429_classified_as_rate_limit(self) -> None:
        c = self._make_client_with_fake_transport(429, b'{"error":{"message":"slow down"}}')
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMRateLimitError):
            asyncio.run(
                c.complete(
                    system="s",
                    user="u",
                    timeout_s=5.0,
                    cancellation=cm,
                )
            )

    def test_500_classified_as_unavailable(self) -> None:
        c = self._make_client_with_fake_transport(500, b'{"error":"server error"}')
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMUnavailableError):
            asyncio.run(
                c.complete(
                    system="s",
                    user="u",
                    timeout_s=5.0,
                    cancellation=cm,
                )
            )

    def test_400_classified_as_response_error(self) -> None:
        c = self._make_client_with_fake_transport(400, b'{"error":{"message":"bad request"}}')
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMResponseError):
            asyncio.run(
                c.complete(
                    system="s",
                    user="u",
                    timeout_s=5.0,
                    cancellation=cm,
                )
            )

    def test_successful_response_parsed(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": '{"action": "finish", "args": {}}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "gpt-4o-mini",
            }
        ).encode()
        c = self._make_client_with_fake_transport(200, body)
        cm = CancellationManager(task_id="t")
        resp = asyncio.run(
            c.complete(
                system="you are a planner",
                user="state here",
                timeout_s=5.0,
                cancellation=cm,
            )
        )
        assert resp.provider == "openai"
        assert resp.model == "gpt-4o-mini"
        assert resp.finish_reason == "stop"
        assert resp.usage is not None
        # parsed_json should work on the content
        parsed = resp.parsed_json
        assert parsed["action"] == "finish"

    def test_empty_content_raises_response_error(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        ).encode()
        c = self._make_client_with_fake_transport(200, body)
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMResponseError):
            asyncio.run(
                c.complete(
                    system="s", user="u", timeout_s=5.0, cancellation=cm
                )
            )

    def test_no_choices_raises_response_error(self) -> None:
        body = b'{"choices": []}'
        c = self._make_client_with_fake_transport(200, body)
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMResponseError):
            asyncio.run(
                c.complete(
                    system="s", user="u", timeout_s=5.0, cancellation=cm
                )
            )

    def test_vision_call_uses_image_content(self) -> None:
        """Vision-capable client sends image_url in the message content."""
        import httpx

        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured["payload"] = payload
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": '{"action": "finish", "args": {}}'},
                                "finish_reason": "stop",
                            }
                        ],
                        "model": "gpt-4o",
                    }
                ).encode(),
            )

        transport = httpx.MockTransport(_handler)
        c = OpenAICompatLLMClient(
            api_key="sk-test",
            base_url="https://fake.test/v1",
            model="gpt-4o",
            vision_capable=True,
        )
        c._client = httpx.AsyncClient(
            base_url="https://fake.test/v1",
            headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
            transport=transport,
            timeout=httpx.Timeout(30.0),
        )
        cm = CancellationManager(task_id="t")
        resp = asyncio.run(
            c.complete_with_vision(
                system="s",
                user="describe this",
                image_png=b"\x89PNG\r\n\x1a\n fakepng",
                timeout_s=5.0,
                cancellation=cm,
            )
        )
        assert resp.provider == "openai"
        # Verify the request contained an image_url content part.
        messages = captured["payload"]["messages"]
        user_msg = messages[-1]
        assert isinstance(user_msg["content"], list)
        assert any(part.get("type") == "image_url" for part in user_msg["content"])

    def test_vision_call_without_vision_capability_raises(self) -> None:
        c = OpenAICompatLLMClient(
            api_key="sk-test",
            base_url="https://fake.test/v1",
            model="gpt-3.5-turbo",
            vision_capable=False,
        )
        cm = CancellationManager(task_id="t")
        with pytest.raises(LLMUnavailableError):
            asyncio.run(
                c.complete_with_vision(
                    system="s",
                    user="u",
                    image_png=b"fake",
                    timeout_s=5.0,
                    cancellation=cm,
                )
            )


# ---------------------------------------------------------------------------
# Provider-agnostic planner behavior
# ---------------------------------------------------------------------------


def _make_state() -> BrowserState:
    state = BrowserState(task_id="t", target="http://127.0.0.1:9999/")
    state.current_url = "http://127.0.0.1:9999/"
    state.dom_hash = "abc123"
    state.technologies = []
    state.interactive_elements = [
        InteractiveElement(ref="button#1:Login", role="button", name="Login", tag="button"),
        InteractiveElement(ref="textbox#1:User", role="textbox", name="User", tag="input", type="text"),
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


class TestProviderAgnosticPlanner:
    """The LLMTaskPlanner must work identically regardless of which LLMClient
    implementation it receives. It never references ZaiLLMClient or
    OpenAICompatLLMClient directly — only the LLMClient interface."""

    async def test_planner_works_with_mock_client(self) -> None:
        """The planner drives correctly with any MockLLMClient."""
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
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step.kind == "finish"
        assert planner.stats.llm_succeeded == 1

    async def test_planner_does_not_reference_concrete_client_class(self) -> None:
        """Verify the planner code only uses the LLMClient interface.

        We do this by passing a minimal duck-typed client (not a subclass of
        any concrete implementation) and confirming the planner still works.
        """
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()

        class _MinimalClient:
            """Duck-typed LLMClient — not a subclass of any concrete impl."""
            provider_name = "minimal"

            def available(self) -> bool:
                return True

            @property
            def supports_vision(self) -> bool:
                return False

            async def complete(self, *, system, user, timeout_s, cancellation, **kw):
                cancellation.check()
                return LLMResponse(
                    content=json.dumps({"action": "finish", "args": {}}),
                    provider="minimal",
                )

            async def complete_with_vision(self, *, system, user, image_png, timeout_s, cancellation):
                raise LLMUnavailableError("no vision")

        planner = LLMTaskPlanner(
            settings=settings,
            client=_MinimalClient(),  # type: ignore[arg-type]
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step.kind == "finish"
        assert planner.stats.llm_succeeded == 1

    async def test_planner_falls_back_on_any_llm_error_subclass(self) -> None:
        """All LLM*Error subclasses trigger deterministic fallback."""
        from sera_browser.task.llm_client import (
            LLMAuthError,
            LLMRateLimitError,
            LLMResponseError,
        )
        for exc in [
            LLMUnavailableError("unavail"),
            LLMTimeoutError("timeout"),
            LLMAuthError("auth"),
            LLMRateLimitError("rate"),
            LLMResponseError("bad"),
        ]:
            state = _make_state()
            cm = CancellationManager(task_id=f"t-{exc.__class__.__name__}")
            settings = _make_settings()
            client = MockLLMClient(responses=[exc])
            planner = LLMTaskPlanner(
                settings=settings,
                client=client,
                cancellation=cm,
                objective="test",
                deterministic=TaskPlanner(settings),
            )
            step = await planner.next_step(state, LoopRecoveryManager())
            # All should fall back to deterministic (which produces a navigate
            # step since current_url is set but dom_hash is too).
            assert step.kind in {"navigate", "observe", "finish", "blocked", "explore_routes"}
            assert planner.stats.fallback_to_deterministic == 1

    async def test_vision_escalation_only_on_loop_recovery(self) -> None:
        """Vision is NEVER called during normal planning — only on loop
        recovery AND when the client supports vision AND a screenshot
        provider is configured."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        vision_calls: list[int] = []

        class _VisionTrackingClient(MockLLMClient):
            def __init__(self) -> None:
                super().__init__(_supports_vision=True)
                self.vision_invocations = 0

            async def complete_with_vision(self, *, system, user, image_png, timeout_s, cancellation):
                self.vision_invocations += 1
                vision_calls.append(self.vision_invocations)
                return LLMResponse(
                    content=json.dumps({"action": "finish", "args": {}}),
                    provider="vision-mock",
                )

        client = _VisionTrackingClient()
        # Pre-seed a normal response so the first call (no loop) doesn't
        # escalate to vision.
        client.responses = [json.dumps({"action": "finish", "args": {}})]
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            screenshot_provider=lambda: _async_return(b"fakepng"),
        )
        # Normal step — should NOT trigger vision.
        await planner.next_step(state, LoopRecoveryManager())
        assert len(vision_calls) == 0
        assert planner.stats.vision_calls == 0

    async def test_vision_escalation_triggers_on_semantic_loop(self) -> None:
        """When loop recovery fires AND client.supports_vision AND screenshot
        provider is configured, the planner calls complete_with_vision."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()

        async def _screenshot_provider() -> bytes:
            return b"\x89PNG fake screenshot bytes"

        client = MockLLMClient(
            responses=[
                # Vision response
                json.dumps({"action": "finish", "args": {}}),
            ],
            _supports_vision=True,
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            screenshot_provider=_screenshot_provider,
        )
        loop = LoopRecoveryManager(semantic_loop_limit=3)
        # Force a semantic loop (same dom_hash 3 times).
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        step = await planner.next_step(state, loop)
        assert planner.stats.vision_calls == 1
        assert planner.stats.vision_succeeded == 1
        assert step.kind == "finish"

    async def test_semantic_loop_non_vision_client_redirects_llm(self) -> None:
        """Non-vision client + semantic loop: the planner re-prompts the LLM
        with a page-unchanged directive instead of burning steps on plain
        screenshots (a text model cannot see them, and a screenshot never
        changes dom_hash, so the old detour never cleared the loop)."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(
            _supports_vision=False,  # no vision
            responses=[json.dumps({"action": "navigate", "args": {"url": state.target}})],
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            screenshot_provider=lambda: _async_return(b"fakepng"),
        )
        loop = LoopRecoveryManager(semantic_loop_limit=3)
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        step = await planner.next_step(state, loop)
        # No vision call was made.
        assert planner.stats.vision_calls == 0
        # The LLM was consulted once with the target-abandonment directive.
        assert planner.stats.llm_calls == 1
        assert "LOOP RECOVERY" in client.calls[0]["user"]
        # The semantic-loop budget was reset.
        assert loop.semantic_loop(state) is False
        # The LLM's different action was accepted.
        assert step.action == "navigate"

    async def test_semantic_loop_non_vision_client_llm_down_falls_back(self) -> None:
        """Non-vision client + semantic loop + LLM unavailable: the planner
        degrades to the deterministic planner (no crash, no screenshot spam)."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(_supports_vision=False)  # no responses → exhausted
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        loop = LoopRecoveryManager(semantic_loop_limit=3)
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        step = await planner.next_step(state, loop)
        assert planner.stats.llm_calls == 1  # tried once
        assert planner.stats.fallback_to_deterministic >= 1
        # Some deterministic step — not a crash, not an endless screenshot.
        assert step is not None

    async def test_vision_escalation_falls_back_when_no_screenshot_provider(self) -> None:
        """If loop recovery fires, client supports vision, but no screenshot
        provider is configured, the planner NEVER uses a plain screenshot
        as the loop escape (v4: screenshots are not a loop-escape strategy).
        It degrades to LLM-with-directive or the deterministic coverage
        planner instead."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()
        client = MockLLMClient(_supports_vision=True)  # vision ok
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            screenshot_provider=None,  # no provider
        )
        loop = LoopRecoveryManager(semantic_loop_limit=3)
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        step = await planner.next_step(state, loop)
        assert planner.stats.vision_calls == 0
        # v4 invariant: no screenshot-as-escape, ever.
        assert step.kind != "capture_screenshot"

    async def test_vision_stats_recorded_in_report_observation(self) -> None:
        """LLMPlannerStats includes vision_calls and vision_succeeded."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings()

        async def _screenshot_provider() -> bytes:
            return b"fakepng"

        client = MockLLMClient(
            responses=[json.dumps({"action": "finish", "args": {}})],
            _supports_vision=True,
        )
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
            screenshot_provider=_screenshot_provider,
        )
        loop = LoopRecoveryManager(semantic_loop_limit=3)
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        loop.record_dom_hash("same")
        await planner.next_step(state, loop)
        stats = planner.stats.as_dict()
        assert "vision_calls" in stats
        assert "vision_succeeded" in stats
        assert stats["vision_calls"] == 1
        assert stats["vision_succeeded"] == 1


async def _async_return(value: Any) -> Any:
    """Helper: returns `value` when awaited. Used as a fake screenshot provider."""
    return value


# ---------------------------------------------------------------------------
# Integration: factory + planner end-to-end
# ---------------------------------------------------------------------------


class TestFactoryWithPlannerIntegration:
    async def test_factory_built_zai_client_works_with_planner(self) -> None:
        """A planner built with a factory-constructed ZaiLLMClient should
        behave identically to one built with a direct ZaiLLMClient."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings(llm_provider="zai", llm_model="glm-4-plus")
        client = make_llm_client(settings.to_llm_config())
        # ZaiLLMClient.available() checks if `z-ai` is on PATH.
        # In the test env it may or may not be available — either way the
        # planner must handle it gracefully.
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        # If z-ai CLI is available, the planner will call it; if not, it
        # falls back to deterministic. Either way, no exception.
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step is not None
        if not client.available():
            assert planner.stats.llm_unavailable >= 1

    async def test_factory_built_openai_client_works_with_planner_when_unavailable(self) -> None:
        """A planner built with a factory-constructed OpenAICompatLLMClient
        that has no api_key should fall back to deterministic."""
        state = _make_state()
        cm = CancellationManager(task_id="t")
        settings = _make_settings(
            llm_provider="openai", llm_model="gpt-4o-mini", llm_api_key=""
        )
        client = make_llm_client(settings.to_llm_config())
        assert client.available() is False  # no api key
        planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cm,
            objective="test",
            deterministic=TaskPlanner(settings),
        )
        step = await planner.next_step(state, LoopRecoveryManager())
        assert step is not None
        assert planner.stats.llm_unavailable >= 1
        assert planner.stats.fallback_to_deterministic >= 1
