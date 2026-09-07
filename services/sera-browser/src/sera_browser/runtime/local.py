"""LocalPlaywrightRuntime — the fully-supported local backend.

A thin, behavior-preserving adapter over ``PlaywrightDriver`` + ``Page``:
the agent's execution core (registry, executor, planner, coverage) keeps
talking to the driver it was rebuilt on, while satisfying the
BrowserRuntime protocol for callers that want backend independence.

Locator strategy resolution stays in the ElementRegistry (the single
authoritative namespace): this runtime resolves refs through the registry
so every action is a FRESH locator resolution with the bounded budget.
"""
from __future__ import annotations

from typing import Any

from ..browser.driver import PlaywrightDriver
from ..browser.element_registry import ElementRegistry
from ..config import Settings
from ..logging import get_logger
from ..models import InteractiveElement
from ..task.failure import ActionFailure

log = get_logger("sera.runtime.local")


class LocalPlaywrightRuntime:
    """BrowserRuntime over PlaywrightDriver + Page.

    ``page`` stays exposed (viewer, tabs, screenshots, storage need the
    real Playwright objects) — this is the primary agent backend.
    """

    name = "local-playwright"

    def __init__(
        self,
        settings: Settings,
        driver: PlaywrightDriver,
        page: Any,
        registry: ElementRegistry,
    ) -> None:
        self.settings = settings
        self.driver = driver
        self.page = page
        self.registry = registry

    # -- lifecycle -------------------------------------------------------

    async def health(self) -> bool:
        try:
            _ = self.page.url
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- navigation ------------------------------------------------------

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        timing = await self.driver.navigate(self.page, url, wait_until=wait_until)
        return {"ok": True, "ms": timing.total_ms, "url": self.page.url}

    async def back(self) -> bool:
        return await self.driver.back(self.page)

    async def forward(self) -> bool:
        return await self.driver.forward(self.page)

    async def reload(self) -> dict[str, Any]:
        timing = await self.driver.reload(self.page)
        return {"ok": True, "ms": timing.total_ms, "url": self.page.url}

    # -- observation -----------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        snap = await self.driver.snapshot(self.page, registry=self.registry)
        return {
            "ok": True,
            "url": snap.url,
            "title": snap.title,
            "dom_hash": snap.dom_hash,
            "elements": [e.model_dump() for e in snap.elements],
        }

    async def find_links(self) -> list[dict[str, str]]:
        return await self.driver.find_links(self.page)

    async def find_forms(self) -> list[dict[str, Any]]:
        return await self.driver.find_forms(self.page)

    async def detect_technologies(self) -> list[str]:
        return await self.driver.detect_technologies(self.page)

    # -- actions (fresh registry resolution + single budget) -------------

    async def click(self, ref: str) -> dict[str, Any]:
        loc = await self.registry.resolve(self.page, ref)
        if loc is None:
            return {"ok": False, "failure": ActionFailure(kind="not_found", detail=ref)}
        ok = await self.driver.click_locator(self.page, loc)
        return {"ok": ok, "failure": None if ok else ActionFailure(kind="timeout", detail=ref)}

    async def fill(self, ref: str, value: str) -> dict[str, Any]:
        loc = await self.registry.resolve(self.page, ref)
        if loc is None:
            return {"ok": False, "failure": ActionFailure(kind="not_found", detail=ref)}
        ok = await self.driver.fill_locator(self.page, loc, value)
        return {"ok": ok, "failure": None if ok else ActionFailure(kind="timeout", detail=ref)}

    async def select_option(self, ref: str, value: str) -> dict[str, Any]:
        loc = await self.registry.resolve(self.page, ref)
        if loc is None:
            return {"ok": False, "failure": ActionFailure(kind="not_found", detail=ref)}
        ok = await self.driver.select_locator(self.page, loc, value)
        return {"ok": ok, "failure": None if ok else ActionFailure(kind="timeout", detail=ref)}

    async def set_checked(self, ref: str, checked: bool) -> dict[str, Any]:
        loc = await self.registry.resolve(self.page, ref)
        if loc is None:
            return {"ok": False, "failure": ActionFailure(kind="not_found", detail=ref)}
        ok = await self.driver.check_locator(self.page, loc, checked)
        return {"ok": ok, "failure": None if ok else ActionFailure(kind="timeout", detail=ref)}

    async def hover(self, ref: str) -> dict[str, Any]:
        loc = await self.registry.resolve(self.page, ref)
        if loc is None:
            return {"ok": False, "failure": ActionFailure(kind="not_found", detail=ref)}
        ok = await self.driver.hover_locator(self.page, loc)
        return {"ok": ok, "failure": None if ok else ActionFailure(kind="timeout", detail=ref)}

    async def scroll(self, dy: int = 400) -> bool:
        return await self.driver.scroll(self.page, dy=dy)

    async def press(self, key: str) -> bool:
        return await self.driver.keyboard_press(self.page, key)

    async def mouse_click(self, x: float, y: float) -> bool:
        return await self.driver.mouse_click(self.page, x, y)

    # -- state -----------------------------------------------------------

    async def get_current_url(self) -> str:
        return await self.driver.get_current_url(self.page)

    async def get_title(self) -> str:
        return await self.driver.get_title(self.page)

    async def list_storage_keys(self, kind: str) -> list[str]:
        return await self.driver.list_storage_keys(self.page, kind)


__all__ = ["LocalPlaywrightRuntime"]
