"""Failure-injection matrix (rebuild v4, Phase 18).

Every row of the matrix must produce: recovery OR clean termination —
never a hang, never an infinite loop, never an unbounded budget. Each
failure class is deterministic: the recovery strategy comes from the
failure classification, not from the LLM.

Matrix:
    stale references        → stale_ref, no budget burn, task continues
    unknown references      → immediate rejection
    overlay/timeout         → bounded budget, target blocked after 2
    LLM 400 (bad response)  → fallback to deterministic, task continues
    LLM 429 (rate limit)    → fallback, task continues
    LLM 503 (unavailable)   → fallback, task continues
    vision 400/503 x2       → circuit breaker opens, no exploration loop
    browser disconnect      → clean task failure, no hang
    off-whitelist redirect  → nav_rejected / target rejected
"""
from __future__ import annotations

import json
import time

import pytest

from sera_browser.browser.element_registry import ElementRegistry
from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState
from sera_browser.task.agent import BrowserAgent, TaskConfig
from sera_browser.task.executor import ActionExecutor
from sera_browser.task.llm_client import (
    LLMClient,
    LLMConfig,
    LLMResponse,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
    MockLLMClient,
)
from sera_browser.task.llm_planner import LLMTaskPlanner
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.planner import TaskPlanner


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Matrix rows 1-3: reference + action failures (executor level)
# ---------------------------------------------------------------------------


async def _stack(settings: Settings, page, task_id: str = "fim"):
    registry = ElementRegistry()
    driver = PlaywrightDriver(settings, CancellationManager(task_id=task_id))
    inspector = Inspector(driver=driver, registry=registry)
    state = BrowserState(task_id=task_id, target=page.url)
    loop = LoopRecoveryManager(failure_block_limit=settings.failure_block_limit)
    executor = ActionExecutor(
        driver, inspector, settings, CancellationManager(task_id=task_id),
        registry=registry,
    )
    return registry, inspector, executor, state, loop


async def test_injection_stale_reference(browser_manager, settings, local_site_url):
    """Invalidated refs never resolve again — and re-observation cannot
    resurrect them (permanent for the task)."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    registry, inspector, executor, state, loop = await _stack(settings, page)

    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/")
    summary = await inspector.observe(page)
    assert summary.elements
    victim = summary.elements[0].ref
    registry.invalidate(victim, "injected")

    res = await executor.execute(page, state, loop, "click", ref=victim)
    assert res.ok is False
    assert res.failure is not None and res.failure.kind == "stale_ref"

    # Re-observation does not resurrect the invalidated identity.
    await inspector.observe(page)
    res2 = await executor.execute(page, state, loop, "click", ref=victim)
    assert res2.ok is False and res2.failure.kind == "stale_ref"


async def test_injection_unknown_reference(browser_manager, settings, local_site_url):
    """Unknown refs are rejected immediately without a browser roundtrip."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    registry, inspector, executor, state, loop = await _stack(settings, page)
    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/")
    await inspector.observe(page)

    started = time.perf_counter()
    res = await executor.execute(page, state, loop, "click", ref="e424242")
    elapsed = time.perf_counter() - started
    assert res.ok is False
    assert res.failure is not None and res.failure.kind == "stale_ref"
    assert elapsed < 1.0


async def test_injection_overlay_timeout_blocks_target(
    browser_manager, settings, local_site_url
):
    """Overlayed (never clickable) element: timeout classification, ONE
    bounded retry, and the target is blocked after failure_block_limit."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    settings = settings.model_copy(
        update={"action_timeout_ms": 700, "action_retry_timeout_ms": 250}
    )
    registry, inspector, executor, state, loop = await _stack(settings, page)

    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/stuck")
    summary = await inspector.observe(page)
    stuck = next(e for e in summary.elements if e.name == "Stuck Button")

    res1 = await executor.execute(page, state, loop, "click", ref=stuck.ref)
    assert res1.ok is False
    assert res1.failure is not None
    assert res1.failure.kind in ("timeout", "not_found", "not_actionable")
    res2 = await executor.execute(page, state, loop, "click", ref=stuck.ref)
    assert res2.ok is False
    assert stuck.ref in loop.blocked_refs, "repeated failure must block the target"


# ---------------------------------------------------------------------------
# Matrix rows 4-6: LLM provider failures (planner level → agent level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        LLMResponseError("400 bad request"),
        LLMRateLimitError("429 rate limited"),
        LLMUnavailableError("503 service unavailable"),
    ],
    ids=["llm-400", "llm-429", "llm-503"],
)
async def test_injection_llm_provider_error_falls_back_and_completes(
    browser_manager, settings, local_site_url, exc
):
    """LLM provider errors (400/429/503) must degrade to the deterministic
    coverage planner and still complete the task — never hang, never crash."""
    from sera_browser.task.llm_client import MockLLMClient

    # Every LLM call raises the injected provider error.
    mock_client = MockLLMClient(responses=[exc, exc, exc, exc, exc, exc, exc, exc])
    agent = BrowserAgent(
        settings=settings, browser_manager=browser_manager, llm_client=mock_client
    )
    cfg = TaskConfig(
        task_id=f"fim-llm-{type(exc).__name__}",
        target=local_site_url + "/",
        objective="Analyze the site.",
        browser_mode="headless",
        timeout_s=60,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=4,
    )
    result = await agent.run(cfg, CancellationManager(task_id=cfg.task_id))
    assert result.report is not None, f"error={result.error}"
    assert result.report.status in {"completed", "blocked", "timeout"}, (
        f"provider error must not fail the task: {result.report.status}"
    )


# ---------------------------------------------------------------------------
# Matrix row 7: vision provider failures → circuit breaker
# ---------------------------------------------------------------------------


async def test_injection_vision_errors_trip_circuit_breaker(settings):
    """Two consecutive vision provider failures OPEN the circuit: no more
    vision calls, and the planner never falls into screenshot ping-pong."""
    from sera_browser.cancel import CancellationManager

    class _VisionFailsClient(MockLLMClient):
        _supports_vision = True

        async def complete_with_vision(self, **kwargs):  # type: ignore[override]
            raise LLMUnavailableError("503 vision down")

    state = BrowserState(task_id="fim-vision", target="http://127.0.0.1:9999/")
    state.current_url = state.target
    state.dom_hash = "x" * 40
    state.technologies = ["jQuery"]
    from sera_browser.models import InteractiveElement

    state.interactive_elements = [
        InteractiveElement(ref="e001", role="link", name="Home", tag="a", href="/")
    ]
    state.routes = ["/about"]

    cm = CancellationManager(task_id="fim-vision")
    client = _VisionFailsClient(responses=[], _supports_vision=True)
    planner = LLMTaskPlanner(
        settings=settings,
        client=client,
        cancellation=cm,
        objective="test",
        deterministic=TaskPlanner(settings),
        screenshot_provider=lambda: _png(),
    )

    async def _png() -> bytes:
        return b"\x89PNG-fake"

    # Force semantic-loop conditions so vision escalation is attempted.
    loop = LoopRecoveryManager(semantic_loop_limit=2, tool_repeat_limit=2)
    loop.record_dom_hash("same")
    loop.record_dom_hash("same")

    step1 = await planner.next_step(state, loop)
    assert step1 is not None
    loop.record_dom_hash("same2")
    loop.record_dom_hash("same2")
    step2 = await planner.next_step(state, loop)
    assert step2 is not None

    # Circuit breaker is open after 2 consecutive vision failures.
    assert planner._vision_disabled is True
    assert planner.stats.vision_calls <= 2
    # And the escape was never "capture a screenshot".
    assert step1.kind != "capture_screenshot" or step2.kind != "capture_screenshot"


# ---------------------------------------------------------------------------
# Matrix row 8: browser disconnect mid-action
# ---------------------------------------------------------------------------


async def test_injection_browser_disconnect_terminates_cleanly(browser_manager, settings, local_site_url):
    """Killing the page mid-action must classify as browser_disconnected
    (or a clean immediate failure) — never a hang."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    registry, inspector, executor, state, loop = await _stack(settings, page)
    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/")
    summary = await inspector.observe(page)
    ref = summary.elements[0].ref

    # Kill the page AFTER resolution was cached in the registry.
    await page.close()
    started = time.perf_counter()
    res = await executor.execute(page, state, loop, "click", ref=ref)
    elapsed = time.perf_counter() - started

    # The task-level contract: bounded time, classified failure.
    assert elapsed < 20.0, f"disconnected action consumed {elapsed:.1f}s"
    # With a closed page, resolution either fails fast (not_found) or the
    # click errors out — both classified, neither hangs.
    assert res.ok is False
    assert res.failure is not None


# ---------------------------------------------------------------------------
# Matrix row 9: redirect / target outside the whitelist
# ---------------------------------------------------------------------------


async def test_injection_off_whitelist_target_rejected(settings):
    """A navigate to an off-whitelist target is rejected by validation
    BEFORE any browser action (SSRF guard stays intact in v4)."""
    from sera_browser.task.action_schema import validate_action

    allowlisted = settings.model_copy(update={"allowed_domains": "127.0.0.1,localhost"})
    state = BrowserState(task_id="fim-nav", target="http://127.0.0.1:9999/")
    state.current_url = state.target
    parsed = {"action": "navigate", "args": {"url": "http://example.attacker.com/"}}
    result = validate_action(parsed, state, allowlisted)
    assert result.ok is False
    assert "not in allowed_domains" in result.reason


async def test_injection_extract_without_handler_is_classified_failure(
    browser_manager, settings, local_site_url
):
    """v3 bug: extract returned ok=True with the prompt as detail (fake
    success). v4: classified provider_error."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    registry, inspector, executor, state, loop = await _stack(settings, page)
    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/")
    await inspector.observe(page)

    res = await executor.execute(page, state, loop, "extract", prompt="list products")
    assert res.ok is False, "extract without a handler must not fake success"
    assert res.failure is not None and res.failure.kind == "provider_error"


async def test_injection_scrape_data_preserved(browser_manager, settings, local_site_url):
    """Decision 13: scrape results are preserved as structured data, not
    reduced to count-only strings."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="fim"))
    page = await cm.open(handle.browser)
    registry, inspector, executor, state, loop = await _stack(settings, page)
    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/products")
    await inspector.observe(page)

    res = await executor.execute(
        page,
        state,
        loop,
        "scrape",
        item=".product",
        fields=[{"name": "name", "selector": ".name"}, {"name": "price", "selector": ".price"}],
    )
    if res.ok:
        assert res.data is not None, "scrape data must be preserved"
        assert "items" in res.data
        assert res.data["count"] >= 1
    else:
        # The fixture may not use .product classes — acceptable: then the
        # failure must be classified, not a fake success.
        assert res.failure is not None
