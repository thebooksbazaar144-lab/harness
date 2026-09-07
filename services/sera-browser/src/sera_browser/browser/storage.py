"""StorageManager — cookies + localStorage/sessionStorage + storage_state.

Credentials are NEVER logged. Saved `storage_state` is treated as sensitive
(chmod 0o600, .gitignore'd).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger, redact
from .context import ContextManager

log = get_logger("sera.browser.storage")


class StorageManager:
    def __init__(
        self,
        settings: Settings,
        context_manager: ContextManager,
        cancellation: CancellationManager,
    ) -> None:
        self.settings = settings
        self.cm = context_manager
        self.cancellation = cancellation

    async def get_cookies(self, page: Page) -> list[dict[str, Any]]:
        """Get cookies. Names are kept (rarely sensitive); values are redacted
        unless `log_sensitive` is True."""
        self.cancellation.check()
        try:
            ctx = page.context
            cookies = await self.cancellation.wait_or_cancel(ctx.cookies(), timeout=5.0)
            if not self.settings.log_sensitive:
                cookies = [
                    {**c, "value": "***REDACTED***"} if "value" in c else c for c in cookies
                ]
            return cookies
        except PlaywrightError as e:
            log.warning("get cookies failed", extra={"event": "cookies.fail", "err": str(e)})
            return []

    async def add_cookies(self, page: Page, cookies: list[dict[str, Any]]) -> bool:
        """Attach cookies to the context."""
        self.cancellation.check()
        try:
            await page.context.add_cookies(cookies)
            log.info(
                "added cookies",
                extra={"event": "cookies.add", "count": len(cookies)},
            )
            return True
        except PlaywrightError as e:
            log.warning("add cookies failed", extra={"event": "cookies.add.fail", "err": str(e)})
            return False

    async def list_storage_keys(self, page: Page) -> tuple[list[str], list[str]]:
        """Return (localStorage keys, sessionStorage keys)."""
        ls = await self._safe_keys(page, "localStorage")
        ss = await self._safe_keys(page, "sessionStorage")
        return ls, ss

    async def _safe_keys(self, page: Page, kind: str) -> list[str]:
        try:
            return await self.cancellation.wait_or_cancel(
                page.evaluate(f"() => Array.from({kind}.keys())"), timeout=5.0
            )
        except PlaywrightError:
            return []

    async def save_state(self, path: Path) -> bool:
        await self.cm.save_storage_state(path)
        return path.exists()
