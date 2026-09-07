"""Screenshot + download + popup tests."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.screenshots import ScreenshotLayer
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState


pytestmark = pytest.mark.asyncio


async def test_screenshot_captured(browser_manager, settings, local_site_url, tmp_path):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        layer = ScreenshotLayer(tmp_path, CancellationManager(task_id="t"))
        path = await layer.capture(page, label="test")
        assert path is not None
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0
    finally:
        await cm.close()


async def test_download_recorded(browser_manager, settings, local_site_url):
    """Trigger /download; ensure BrowserState.downloads is populated."""
    from sera_browser.task.agent import BrowserAgent

    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="t"))
    page = await cm.open(handle.browser)
    try:
        state = BrowserState(task_id="t", target=local_site_url)

        # Wire download event into state.
        def _on_download(dl):
            from sera_browser.models import DownloadRecord
            state.downloads.append(
                DownloadRecord(suggested_filename=dl.suggested_filename, url=dl.url)
            )

        page.on("download", _on_download)

        driver = PlaywrightDriver(settings, CancellationManager(task_id="t"))
        await driver.navigate(page, local_site_url + "/")
        # Click the download link via direct navigation.
        await page.evaluate("""async () => {
            const a = document.createElement('a');
            a.href = '/download';
            a.download = 'test.txt';
            document.body.appendChild(a);
            a.click();
        }""")
        for _ in range(40):
            if state.downloads:
                break
            await asyncio.sleep(0.1)
        assert state.downloads, "download not recorded"
        assert state.downloads[0].suggested_filename == "test.txt"
    finally:
        await cm.close()
