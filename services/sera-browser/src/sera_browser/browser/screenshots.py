"""ScreenshotLayer — vision escalation only.

Screenshots are captured on-demand and saved under the task session dir.
They are referenced by path in the final report. We do NOT send them to a
vision model on every step.
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, Page

from ..cancel import CancellationManager
from ..logging import get_logger

log = get_logger("sera.browser.screenshot")


class ScreenshotLayer:
    def __init__(self, session_dir: Path, cancellation: CancellationManager) -> None:
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.cancellation = cancellation
        self._counter = 0

    async def capture(self, page: Page, *, label: str = "step", full_page: bool = True) -> Path | None:
        """Capture a screenshot. Returns the saved path or None on failure."""
        self.cancellation.check()
        self._counter += 1
        ts = int(time.time())
        path = self.session_dir / f"{self._counter:03d}_{label}_{ts}.png"
        try:
            await self.cancellation.wait_or_cancel(
                page.screenshot(path=str(path), full_page=full_page),
                timeout=15.0,
            )
            log.info(
                "screenshot captured",
                extra={"event": "screenshot.captured", "path": str(path), "label": label},
            )
            return path
        except (PlaywrightError, Exception) as e:  # noqa: BLE001
            log.warning(
                "screenshot failed",
                extra={"event": "screenshot.fail", "err": str(e)},
            )
            return None

    async def capture_bytes(self, page: Page) -> bytes | None:
        """Return raw PNG bytes (e.g. for vision-model escalation)."""
        self.cancellation.check()
        try:
            return await self.cancellation.wait_or_cancel(
                page.screenshot(full_page=False), timeout=10.0
            )
        except (PlaywrightError, Exception) as e:  # noqa: BLE001
            log.warning("screenshot bytes failed", extra={"event": "screenshot.bytes.fail", "err": str(e)})
            return None
