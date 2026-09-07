"""ContextManager — isolated Playwright context per task.

- One isolated context per task/session (no cross-task sharing)
- Supports pre-authenticated storage_state (treated as sensitive)
- Supports extra headers (bearer tokens, custom auth)
- Tracks pages/popups so cancellation can clean them up
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
)

from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger

log = get_logger("sera.browser.context")


@dataclass
class ContextManager:
    settings: Settings
    cancellation: CancellationManager
    context: BrowserContext | None = None
    pages: list[Page] = field(default_factory=list)
    primary_page: Page | None = None
    storage_state_path: Path | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    async def open(
        self,
        browser,
        *,
        storage_state_path: Path | None = None,
        extra_headers: dict[str, str] | None = None,
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None,
    ) -> Page:
        """Open an isolated context + primary page. Returns the primary page.

        If `settings.stealth_level != "off"`, applies stealth fingerprint
        randomization (UA, viewport, timezone, locale, proxy) and injects
        anti-bot init scripts into the primary page.
        """
        self.cancellation.check()
        self.storage_state_path = storage_state_path
        if extra_headers:
            self.extra_headers = dict(extra_headers)

        # Build stealth config from settings (if enabled).
        from .stealth import StealthLayer, StealthConfig
        stealth_config = self.settings.to_stealth_config()
        stealth = StealthLayer(stealth_config)
        stealth_fp = stealth.choose_fingerprint() if stealth_config.enabled else None

        ctx_kwargs: dict[str, Any] = {
            "viewport": viewport or (stealth_fp.viewport if stealth_fp else {"width": 1280, "height": 800}),
            "user_agent": user_agent or (stealth_fp.user_agent if stealth_fp else None),
            "ignore_https_errors": False,
            "java_script_enabled": True,
        }
        # Apply stealth-derived context kwargs (timezone, locale, proxy, headers).
        if stealth_fp is not None:
            stealth_kwargs = stealth.context_kwargs(stealth_fp)
            # Merge, but don't override explicitly-passed values.
            for k, v in stealth_kwargs.items():
                if k == "viewport" and viewport:
                    continue  # explicit viewport wins
                if k == "user_agent" and user_agent:
                    continue  # explicit UA wins
                ctx_kwargs[k] = v
        if storage_state_path and storage_state_path.exists():
            ctx_kwargs["storage_state"] = str(storage_state_path)
            log.info(
                "attaching storage_state",
                extra={"event": "context.storage_state", "path": str(storage_state_path)},
            )
        if extra_headers:
            ctx_kwargs["extra_http_headers"] = self._redact_headers(extra_headers)

        self.context = await browser.new_context(**ctx_kwargs)
        self.context.set_default_timeout(self.settings.default_timeout_ms)
        self.context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)

        # Track popups so we can close them on cleanup.
        def _on_page(page: Page) -> None:
            self.pages.append(page)
            log.debug("popup/tab opened", extra={"event": "context.popup", "url": page.url})

        self.context.on("page", _on_page)

        self.primary_page = await self.context.new_page()
        self.pages.append(self.primary_page)

        # Apply stealth init scripts to the primary page (AFTER creation,
        # BEFORE navigation).
        if stealth_fp is not None:
            await stealth.apply(self.primary_page, stealth_fp)

        return self.primary_page

    async def save_storage_state(self, path: Path) -> None:
        """Persist current auth state. Treated as sensitive (chmod 0o600)."""
        if self.context is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.context.storage_state(path=str(path))
            try:
                path.chmod(0o600)
            except OSError:
                pass
            log.info(
                "saved storage_state",
                extra={"event": "context.save_state", "path": str(path)},
            )
        except PlaywrightError as e:
            log.warning("save storage_state failed", extra={"event": "context.save_state.fail", "err": str(e)})

    async def close(self) -> None:
        """Cooperative close. Best-effort; force_cleanup is the safety net."""
        ctx = self.context
        self.context = None
        if ctx is None:
            return
        for page in list(self.pages):
            with suppress(PlaywrightError):
                await page.close()
        self.pages.clear()
        self.primary_page = None
        with suppress(PlaywrightError):
            await ctx.close()

    async def force_close(self) -> None:
        """Final safety net. Should never raise."""
        if self.context is not None:
            for page in list(self.pages):
                with suppress(Exception):  # noqa: BLE001
                    await page.close()
            with suppress(Exception):  # noqa: BLE001
                await self.context.close()
        self.context = None
        self.pages.clear()
        self.primary_page = None

    @staticmethod
    def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
        # Headers we set on the context. We log them redacted elsewhere; the
        # actual values must reach the browser.
        return dict(headers)


# Re-export to avoid the unused import warning in static checks.
__all__ = ["ContextManager", "Playwright"]
