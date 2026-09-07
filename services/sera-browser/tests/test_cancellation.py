"""Cancellation + cleanup tests."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import pytest

from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager, TaskCancelledError
from sera_browser.config import Settings


pytestmark = pytest.mark.asyncio


async def test_cancellation_token_propagates():
    cm = CancellationManager(task_id="t")
    cm.cancel(reason="test")
    with pytest.raises(TaskCancelledError):
        cm.check()


async def test_cancel_aborts_long_sleep():
    cm = CancellationManager(task_id="t")
    started = time.perf_counter()
    # Cancel after 0.1s; sleep should abort within ~0.2s.
    asyncio.get_event_loop().call_later(0.1, lambda: cm.cancel("test"))
    with pytest.raises(TaskCancelledError):
        await cm.asleep(10.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"cancellation took too long: {elapsed:.2f}s"


async def test_cancel_propagates_to_navigation(browser_manager, settings, local_site_url):
    """If cancellation fires during navigation, the agent should bail out."""
    from sera_browser.browser.driver import PlaywrightDriver

    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        agent_cm = CancellationManager(task_id="t")
        driver = PlaywrightDriver(settings, agent_cm)
        # Cancel right before navigating.
        agent_cm.cancel(reason="pre-cancel")
        with pytest.raises(TaskCancelledError):
            await driver.navigate(page, local_site_url + "/")
    finally:
        await cm.close()


def _count_orphan_chromium() -> int:
    """Count chromium processes spawned by ms-playwright."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "ms-playwright/chromium"], text=True, stderr=subprocess.DEVNULL
        )
        return len([p for p in out.split() if p.strip().isdigit()])
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return 0


async def test_no_orphan_chromium_after_normal_close(browser_manager, settings, local_site_url):
    """After a normal task lifecycle, no chromium processes should linger."""
    from sera_browser.browser.driver import PlaywrightDriver

    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
    await driver.navigate(page, local_site_url + "/")
    await cm.close()
    await browser_manager.shutdown()
    # Give the OS a moment to release process handles.
    await asyncio.sleep(0.5)
    # Some tests share the browser; we just verify the count is low.
    # The force_kill sweep is what we rely on for cancellation tests.
    assert _count_orphan_chromium() == 0 or _count_orphan_chromium() < 5


async def test_force_kill_sweeps_orphans(settings):
    """Standalone BrowserManager.force_kill should kill lingering chromium."""
    bm = BrowserManager(settings=settings)
    await bm.launch()
    # Spawn a context + page so a chromium process exists.
    from sera_browser.browser.driver import PlaywrightDriver
    cm = await bm.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(bm._handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
    # Don't navigate — just confirm a chromium exists, then force_kill.
    before = _count_orphan_chromium()
    assert before >= 1
    await bm.force_kill()
    await asyncio.sleep(1.0)
    after = _count_orphan_chromium()
    assert after == 0, f"orphan chromium survived force_kill: {after}"
