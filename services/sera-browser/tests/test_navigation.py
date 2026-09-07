"""Navigation tests: navigate, same-URL idempotency, back, forward, reload."""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings


pytestmark = pytest.mark.asyncio


async def _open_page(browser_manager, settings):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="test"))
    page = await cm.open(handle.browser)
    return page, cm


async def test_navigate_to_local_site(browser_manager, settings, local_site_url):
    page, cm = await _open_page(browser_manager, settings)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        timing = await driver.navigate(page, local_site_url + "/")
        assert page.url.startswith(local_site_url)
        assert timing.total_ms is not None
        assert timing.total_ms >= 0
    finally:
        await cm.close()


async def test_same_url_navigation_is_idempotent(browser_manager, settings, local_site_url):
    """Calling navigate(same_url) twice should NOT reload."""
    page, cm = await _open_page(browser_manager, settings)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        # Inject a marker into the DOM that survives only if we DON'T reload.
        await page.evaluate("() => { window.__MARKER__ = 123; document.title = 'marker-set'; }")
        # Navigate to same URL — should be a no-op (no reload).
        await driver.navigate(page, local_site_url + "/")
        marker = await page.evaluate("() => window.__MARKER__")
        assert marker == 123, "navigate() reloaded the page despite same URL"
    finally:
        await cm.close()


async def test_same_url_force_reloads(browser_manager, settings, local_site_url):
    page, cm = await _open_page(browser_manager, settings)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        await page.evaluate("() => { window.__MARKER__ = 123; }")
        # force=True should reload.
        await driver.navigate(page, local_site_url + "/", force=True)
        marker = await page.evaluate("() => window.__MARKER__")
        assert marker is None, "force=True did not reload"
    finally:
        await cm.close()


async def test_back_forward(browser_manager, settings, local_site_url):
    page, cm = await _open_page(browser_manager, settings)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        await driver.navigate(page, local_site_url + "/login")
        assert "/login" in page.url
        ok = await driver.back(page)
        assert ok
        assert page.url.rstrip("/").endswith(local_site_url.split(":")[-1] + "/") or page.url == local_site_url + "/"
        ok = await driver.forward(page)
        assert ok
        assert "/login" in page.url
    finally:
        await cm.close()


async def test_reload(browser_manager, settings, local_site_url):
    page, cm = await _open_page(browser_manager, settings)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        await page.evaluate("() => { window.__MARKER__ = 1; }")
        timing = await driver.reload(page)
        assert timing.total_ms is not None
        marker = await page.evaluate("() => window.__MARKER__")
        assert marker is None, "reload did not reset page state"
    finally:
        await cm.close()
