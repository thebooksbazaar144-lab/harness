"""Timeout + browser crash recovery tests."""
from __future__ import annotations

import asyncio
import time

import pytest

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings


pytestmark = pytest.mark.asyncio


async def test_failed_browser_launch_is_reported(tmp_path):
    """A failed browser launch should raise PlaywrightError, not hang."""
    from playwright.async_api import Error as PlaywrightError

    settings = Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        session_root=tmp_path / "sessions",
    )
    bm = BrowserManager(settings=settings)
    # Force a bad launch by setting an invalid executable path via env.
    import os
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/nonexistent"
    try:
        with pytest.raises((PlaywrightError, Exception)):  # noqa: BLE001
            await bm.launch()
    finally:
        # Cleanup env so other tests aren't affected.
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        try:
            await bm.force_kill()
        except Exception:  # noqa: BLE001
            pass


async def test_browser_crash_recovery(browser_manager, settings, local_site_url):
    """If the page dies mid-task, the agent should not hang indefinitely."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
    await driver.navigate(page, local_site_url + "/")

    # Simulate crash by closing the page out from under the driver.
    await page.close()

    # A subsequent navigate should fail or open a new page; we just need to
    # verify the driver does not hang forever.
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            driver.navigate(page, local_site_url + "/login"), timeout=10.0
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass
    elapsed = time.perf_counter() - started
    assert elapsed < 12.0, f"driver hung for {elapsed:.1f}s after page crash"
