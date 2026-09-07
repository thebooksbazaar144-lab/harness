"""Multi-tab manager with stable short ids.

Inspired by browser-use's 4-character tab ids and Playwright MCP's
`browser_tabs` tool. Each tab gets a short stable id (e.g. "t1", "t2") that
the LLM uses to reference it. The manager tracks:

- which tab is "active" (the one the LLM is currently operating on)
- tab lifecycle (open/close/switch)
- per-tab URL + title history (for the LLM state view)
- automatic cleanup on task end

The LLM sees a compact `tabs` array in its state view:
    [{"id": "t1", "url": "...", "title": "...", "active": true},
     {"id": "t2", "url": "...", "title": "...", "active": false}]
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from ..logging import get_logger

log = get_logger("sera.browser.tabs")


@dataclass
class TabRecord:
    """A single browser tab/page."""
    id: str  # short stable id like "t1"
    page: Page
    url: str = "about:blank"
    title: str = ""
    active: bool = False
    opened_at: float = 0.0


class TabManager:
    """Manages multiple Playwright pages (tabs) within one BrowserContext.

    Each task gets one TabManager. The primary page is "t1". New popups
    opened by the page are tracked as additional tabs.
    """

    def __init__(self, primary_page: Page) -> None:
        self._tabs: dict[str, TabRecord] = {}
        self._counter = itertools.count(1)
        self._active_id: str | None = None
        # Register the primary page as t1.
        self._register(primary_page, active=True)
        # Wire popup tracking on the primary page (and on any new tabs).
        self._wire_popup_handler(primary_page)

    @property
    def active(self) -> TabRecord | None:
        if self._active_id is None:
            return None
        return self._tabs.get(self._active_id)

    @property
    def active_page(self) -> Page | None:
        rec = self.active
        return rec.page if rec else None

    @property
    def tabs(self) -> list[TabRecord]:
        return list(self._tabs.values())

    def to_view(self) -> list[dict[str, Any]]:
        """Compact JSON-safe view for the LLM state view."""
        return [
            {
                "id": t.id,
                "url": t.url,
                "title": t.title,
                "active": t.active,
            }
            for t in self._tabs.values()
        ]

    def switch_to(self, tab_id: str) -> bool:
        """Activate a tab by id. Returns True on success.

        NOTE: `bring_to_front()` is async in the async API but we call it
        fire-and-forget here — the tab is still activated in Playwright's
        internal state; the visual focus is best-effort.
        """
        if tab_id not in self._tabs:
            return False
        for rec in self._tabs.values():
            rec.active = (rec.id == tab_id)
        self._active_id = tab_id
        try:
            coro = self._tabs[tab_id].page.bring_to_front()
            # Fire-and-forget the coroutine safely.
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(coro)  # type: ignore[arg-type]
                else:
                    loop.run_until_complete(coro)
            except RuntimeError:
                # No event loop — close the coroutine to avoid the warning.
                coro.close()
        except PlaywrightError:
            pass
        log.info(
            "switched tab",
            extra={"event": "tabs.switch", "tab_id": tab_id},
        )
        return True

    def new_tab(self, url: str | None = None) -> TabRecord:
        """Open a new tab. Returns the new TabRecord.

        NOTE: This is a SYNC wrapper that calls `ctx.new_page()` — which in
        the async Playwright API returns a coroutine. Callers MUST use
        `new_tab_async()` instead when working with the async API.
        """
        raise RuntimeError(
            "Use new_tab_async() with the async Playwright API. "
            "new_tab() is only for sync API contexts."
        )

    async def new_tab_async(self, url: str | None = None) -> TabRecord:
        """Open a new tab (async). Returns the new TabRecord."""
        # We need the context to create a new page. Get it from any existing page.
        any_page = next(iter(self._tabs.values())).page
        ctx = any_page.context
        page = await ctx.new_page()
        rec = self._register(page, active=True, url=url or "about:blank")
        self._wire_popup_handler(page)
        if url and url != "about:blank":
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                rec.url = page.url
            except PlaywrightError as e:
                log.warning(
                    "new tab navigation failed",
                    extra={"event": "tabs.new.fail", "url": url, "err": str(e)},
                )
        log.info(
            "opened new tab",
            extra={"event": "tabs.new", "tab_id": rec.id, "url": rec.url},
        )
        return rec

    def close_tab(self, tab_id: str) -> bool:
        """Close a tab by id. Returns True on success.

        If the closed tab was active, activates the most-recently-opened
        remaining tab. Never closes the last tab (Playwright requires at
        least one page per context).
        """
        if tab_id not in self._tabs:
            return False
        if len(self._tabs) <= 1:
            log.warning("refusing to close last tab", extra={"event": "tabs.close.last"})
            return False
        rec = self._tabs[tab_id]
        # `page.close()` is async — fire-and-forget it safely.
        try:
            coro = rec.page.close()
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(coro)  # type: ignore[arg-type]
                else:
                    loop.run_until_complete(coro)
            except RuntimeError:
                coro.close()
        except PlaywrightError:
            pass
        del self._tabs[tab_id]
        if self._active_id == tab_id:
            # Activate the most-recently-added remaining tab.
            remaining = list(self._tabs.values())
            if remaining:
                self.switch_to(remaining[-1].id)
            else:
                self._active_id = None
        log.info(
            "closed tab",
            extra={"event": "tabs.close", "tab_id": tab_id},
        )
        return True

    def update_active_state(self) -> None:
        """Refresh url/title of the active tab. Call after each navigation."""
        rec = self.active
        if rec is None:
            return
        try:
            rec.url = rec.page.url
            rec.title = rec.page.title() if hasattr(rec.page, "title") else ""
        except PlaywrightError:
            pass

    def update_all(self) -> None:
        """Refresh url/title of all tabs."""
        for rec in self._tabs.values():
            try:
                rec.url = rec.page.url
            except PlaywrightError:
                pass

    async def update_all_async(self) -> None:
        """Async version of update_all (uses async title())."""
        for rec in self._tabs.values():
            try:
                rec.url = rec.page.url
                rec.title = await rec.page.title()
            except PlaywrightError:
                pass

    def _register(self, page: Page, *, active: bool, url: str = "") -> TabRecord:
        tab_id = f"t{next(self._counter)}"
        import time

        rec = TabRecord(
            id=tab_id,
            page=page,
            url=url or page.url,
            active=active,
            opened_at=time.time(),
        )
        self._tabs[tab_id] = rec
        if active:
            self._active_id = tab_id
        return rec

    def _wire_popup_handler(self, page: Page) -> None:
        """Wire popup events so new tabs are auto-tracked."""
        def _on_popup(popup: Page) -> None:
            try:
                rec = self._register(popup, active=False)
                log.info(
                    "popup tracked as new tab",
                    extra={"event": "tabs.popup", "tab_id": rec.id},
                )
                self._wire_popup_handler(popup)
            except Exception as e:  # noqa: BLE001
                log.debug("popup tracking failed", extra={"event": "tabs.popup.fail", "err": str(e)})
        page.on("popup", _on_popup)

    async def close_all(self) -> None:
        """Close all tabs except the primary (cleanup)."""
        for tab_id in list(self._tabs.keys()):
            if tab_id == "t1":
                continue
            self.close_tab(tab_id)


__all__ = ["TabManager", "TabRecord"]
