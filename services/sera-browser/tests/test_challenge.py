"""Challenge detection tests (Turnstile test sitekey)."""
from __future__ import annotations

import pytest

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.cancel import CancellationManager
from sera_browser.challenge.detector import ChallengeDetector
from sera_browser.config import Settings


pytestmark = pytest.mark.asyncio


async def test_turnstile_test_sitekey_detected_and_handled(
    browser_manager, settings, local_site_url
):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/challenge")
        det = ChallengeDetector()
        result = await det.detect(page)
        # Turnstile detected
        types = [c.type for c in result.challenges]
        assert "cloudflare_turnstile" in types
        # And it should be marked handled via official test key.
        ts = next(c for c in result.challenges if c.type == "cloudflare_turnstile")
        assert ts.handled is True
        assert ts.handling == "official_test_key"
    finally:
        await cm.close()


async def test_login_form_detected(browser_manager, settings, local_site_url):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/login")
        det = ChallengeDetector()
        result = await det.detect(page)
        assert result.has_login_form
        assert result.login_url == page.url
        types = [c.type for c in result.challenges]
        assert "login_challenge" in types
    finally:
        await cm.close()


async def test_requires_human_returns_none_for_handled(
    browser_manager, settings, local_site_url
):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/challenge")
        det = ChallengeDetector()
        result = await det.detect(page)
        # Turnstile is handled → requires_human should skip it.
        handled_ts = next(
            c for c in result.challenges if c.type == "cloudflare_turnstile" and c.handled
        )
        assert handled_ts
        needs = det.requires_human(result.challenges)
        # The only "needs human" challenge here would be the un-handled
        # Turnstile — but it IS handled, so requires_human may return None
        # or the login_challenge (which we don't treat as human-required).
        if needs is not None:
            assert needs.handled is False
    finally:
        await cm.close()
