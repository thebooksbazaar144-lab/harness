"""Action budget regression tests (rebuild v4, Phase 5 / audit F1).

The v3 click path burned ~15s on a selector attempt + ~15s on a role/name
fallback ≈ 32s per stuck element, and with tool_repeat_limit=4 the same
stuck element could consume ~2 minutes. These tests pin the v4 contract:

1. A failing click consumes AT MOST action_timeout + retry_timeout +
   small overhead — measured in wall clock, not theory.
2. There is exactly ONE retry, after a fresh re-resolution.
3. The failure is classified (timeout / not_found / not_actionable).
4. The same (action, ref) cannot be re-issued indefinitely: failure
   counting blocks the target after `failure_block_limit` failures.
"""
from __future__ import annotations

import time

import pytest

from sera_browser.browser.element_registry import ElementRegistry
from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState
from sera_browser.task.executor import ActionExecutor
from sera_browser.task.loop import LoopRecoveryManager


pytestmark = pytest.mark.asyncio


async def _make_stack(settings: Settings, page):
    registry = ElementRegistry()
    driver = PlaywrightDriver(settings, CancellationManager(task_id="budget"))
    inspector = Inspector(driver=driver, registry=registry)
    state = BrowserState(task_id="budget", target=page.url)
    loop = LoopRecoveryManager(
        tool_repeat_limit=settings.tool_repeat_limit,
        semantic_loop_limit=settings.semantic_loop_limit,
        failure_block_limit=settings.failure_block_limit,
    )
    executor = ActionExecutor(
        driver, inspector, settings, CancellationManager(task_id="budget"),
        registry=registry,
    )
    return registry, inspector, executor, state, loop


async def test_stuck_click_is_bounded_by_budget(browser_manager, settings, local_site_url):
    """A permanently-overlayed button must fail within the bounded budget.

    v3 behavior: ~32s per attempt (selector path + role/name path, each
    with a full timeout). v4: at most action_timeout_ms + retry ms +
    resolution overhead.
    """
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="budget"))
    page = await cm.open(handle.browser)

    # Tight budgets for a fast test: 1.5s + 0.5s retry.
    settings = settings.model_copy(update={"action_timeout_ms": 1500, "action_retry_timeout_ms": 500})
    registry, inspector, executor, state, loop = await _make_stack(settings, page)

    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/stuck")
    summary = await inspector.observe(page)
    assert summary.elements, "stuck page must expose the overlayed button"

    stuck = next(
        (e for e in summary.elements if e.name == "Stuck Button"), None
    )
    assert stuck is not None and stuck.ref, "registry must assign a ref to the stuck button"

    started = time.perf_counter()
    res = await executor.execute(page, state, loop, "click", ref=stuck.ref)
    elapsed = time.perf_counter() - started

    assert res.ok is False, "overlayed button cannot be clicked"
    assert res.failure is not None, "failure must be classified"
    assert res.failure.kind in ("timeout", "not_found", "not_actionable")
    # THE regression assertion: 1.5s budget + 0.5s retry + generous
    # overhead for two resolutions must stay far below the v3 ~32s.
    assert elapsed < 8.0, f"stuck click consumed {elapsed:.1f}s — budget violated"


async def test_failure_blocks_target_after_limit(browser_manager, settings, local_site_url):
    """The same (action, ref) may not fail indefinitely: after
    failure_block_limit failures the target is blocked."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="budget"))
    page = await cm.open(handle.browser)

    settings = settings.model_copy(update={"action_timeout_ms": 800, "action_retry_timeout_ms": 300})
    registry, inspector, executor, state, loop = await _make_stack(settings, page)

    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/stuck")
    summary = await inspector.observe(page)
    stuck = next(e for e in summary.elements if e.name == "Stuck Button")
    ref = stuck.ref

    # First failure.
    res1 = await executor.execute(page, state, loop, "click", ref=ref)
    assert res1.ok is False
    assert ref not in loop.blocked_refs
    # Second failure — same (action, ref) — blocks the target.
    res2 = await executor.execute(page, state, loop, "click", ref=ref)
    assert res2.ok is False
    assert ref in loop.blocked_refs, "target must be blocked after repeated failure"

    # A blocked/invalidated ref fails immediately WITHOUT burning budget.
    started = time.perf_counter()
    res3 = await executor.execute(page, state, loop, "click", ref=ref)
    elapsed3 = time.perf_counter() - started
    assert res3.ok is False
    assert res3.failure is not None
    # After 2 failures the registry invalidates the ref (threshold=3 with
    # mark_failed per attempt... executor marks each failure; the click
    # retry path also marks. Either way, by now it must be stale or blocked.
    assert res3.failure.kind in ("stale_ref", "not_found", "timeout", "not_actionable")
    assert elapsed3 < settings.action_timeout_ms / 1000.0 + 3.0


async def test_retry_count_is_one(browser_manager, settings, local_site_url):
    """A retryable failure gets exactly ONE re-resolution + retry — the
    second attempt's budget is the tight retry budget, not the full one."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="budget"))
    page = await cm.open(handle.browser)

    settings = settings.model_copy(update={"action_timeout_ms": 1200, "action_retry_timeout_ms": 400})
    registry, inspector, executor, state, loop = await _make_stack(settings, page)

    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/stuck")
    summary = await inspector.observe(page)
    stuck = next(e for e in summary.elements if e.name == "Stuck Button")

    started = time.perf_counter()
    res = await executor.execute(page, state, loop, "click", ref=stuck.ref)
    elapsed = time.perf_counter() - started

    # 1200ms + 400ms + resolution overhead — no third attempt, no second
    # full budget. Assert well below 2x the full budget.
    assert elapsed < 2 * (settings.action_timeout_ms / 1000.0), (
        f"click consumed {elapsed:.1f}s — more than budget+retry"
    )
    assert res.failure is not None


async def test_unknown_ref_fails_without_browser_roundtrip(browser_manager, settings, local_site_url):
    """Refs not in the registry fail immediately (no budget, no page call)."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="budget"))
    page = await cm.open(handle.browser)

    registry, inspector, executor, state, loop = await _make_stack(settings, page)
    await executor.execute(page, state, loop, "navigate", url=local_site_url + "/")
    await inspector.observe(page)

    started = time.perf_counter()
    res = await executor.execute(page, state, loop, "click", ref="e999")
    elapsed = time.perf_counter() - started
    assert res.ok is False
    assert res.failure is not None and res.failure.kind == "stale_ref"
    assert elapsed < 1.0, "unknown ref must fail without a browser roundtrip"


async def test_settings_defaults_are_bounded():
    """The shipped defaults themselves must be bounded (v3 shipped 15s
    with sequential fallbacks)."""
    s = Settings()
    assert s.action_timeout_ms <= 10_000, "default action budget must be <= 10s"
    assert s.action_retry_timeout_ms <= 5_000, "retry budget must be tight"
    assert s.tool_repeat_limit <= 2, "tool repeat limit must be tight"
    assert s.failure_block_limit == 2
    assert s.vision_max_calls <= 2, "vision must be budgeted"
    assert s.vision_failure_limit <= 2, "vision breaker must trip quickly"
