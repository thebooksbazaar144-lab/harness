"""BrowserState + DOM/Accessibility inspection tests."""
from __future__ import annotations

import pytest

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState
from sera_browser.state.state import update_from_snapshot


pytestmark = pytest.mark.asyncio


async def _setup(browser_manager, settings, path="/"):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="test"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="test"))
    inspector = Inspector(driver=driver)
    return page, cm, driver, inspector


async def test_snapshot_extracts_interactive_elements(browser_manager, settings, local_site_url):
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        await driver.navigate(page, local_site_url + "/login")
        summary = await inspector.observe(page)
        # login form has at least 1 textbox + 1 button
        roles = {e.role for e in summary.elements}
        assert "textbox" in roles
        assert "button" in roles
        # forms detected
        assert any(f.has_password for f in summary.forms)
    finally:
        await cm.close()


async def test_dom_hash_changes_on_different_pages(browser_manager, settings, local_site_url):
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        await driver.navigate(page, local_site_url + "/")
        s1 = await inspector.observe(page)
        await driver.navigate(page, local_site_url + "/products")
        s2 = await inspector.observe(page)
        assert s1.dom_hash != s2.dom_hash
    finally:
        await cm.close()


async def test_dom_hash_stable_on_same_page(browser_manager, settings, local_site_url):
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        await driver.navigate(page, local_site_url + "/")
        s1 = await inspector.observe(page)
        s2 = await inspector.observe(page)
        assert s1.dom_hash == s2.dom_hash
    finally:
        await cm.close()


async def test_state_update_merges_routes(browser_manager, settings, local_site_url):
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        state = BrowserState(task_id="t", target=local_site_url)
        await driver.navigate(page, local_site_url + "/")
        s1 = await inspector.observe(page)
        update_from_snapshot(state, s1)
        first_routes = set(state.routes)
        # Navigate to another page; routes should accumulate.
        await driver.navigate(page, local_site_url + "/products")
        s2 = await inspector.observe(page)
        update_from_snapshot(state, s2)
        # First-route set should be subset of second.
        assert first_routes.issubset(set(state.routes))
    finally:
        await cm.close()


async def test_validate_selector_rejects_unknown(browser_manager, settings, local_site_url):
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        await driver.navigate(page, local_site_url + "/login")
        summary = await inspector.observe(page)
        # text=Nonexistent should not match.
        assert inspector.validate_selector("text=NonexistentButton", summary.elements) is False
        # text=Sign in should match (login page has a Sign in button).
        assert inspector.validate_selector("text=Sign in", summary.elements) is True
    finally:
        await cm.close()


async def test_technology_detection(browser_manager, settings, local_site_url):
    """The local test site sets window.jQuery and __TEST_SITE__."""
    page, cm, driver, inspector = await _setup(browser_manager, settings)
    try:
        await driver.navigate(page, local_site_url + "/")
        await inspector.observe(page)
        tech = await driver.detect_technologies(page)
        assert "jQuery" in tech
    finally:
        await cm.close()
