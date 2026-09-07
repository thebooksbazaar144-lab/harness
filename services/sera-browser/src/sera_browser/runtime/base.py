"""BrowserRuntime — the execution-surface abstraction (rebuild v4, Phase 3).

```
BrowserRuntime
├── LocalPlaywrightRuntime   (fully supported; wraps PlaywrightDriver + Page)
└── KernelRuntime            (remote kernel browser service via HTTP)
```

Selection: ``SBA_BROWSER_BACKEND=auto|local|kernel`` (Settings.browser_backend).
``auto`` probes the kernel service (``SBA_KERNEL_URL``) and falls back to
local when unhealthy — the local path is never a second-class citizen.

The protocol mirrors the ACTION + OBSERVATION surface the executor and
inspector need. It is deliberately async-method-shaped like the existing
Playwright API (decision 18: no async migration, keep the current style)
and deliberately PAGE-FREE: a runtime owns its browser interaction and
reports results as plain data (elements with locator specs, never
ElementHandles).

The agent currently runs on LocalPlaywrightRuntime (the driver stays the
driver). KernelRuntime is a first-class backend for SERA's session engine
and for agent-level wiring once the kernel service contract is proven —
the wire contract is documented in ``kernel.py`` and pinned by
``tests/test_kernel_runtime.py`` against a stub server.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..config import Settings


@runtime_checkable
class BrowserRuntime(Protocol):
    """The execution surface both backends implement."""

    name: str

    # -- lifecycle -------------------------------------------------------
    async def health(self) -> bool: ...

    # -- navigation ------------------------------------------------------
    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]: ...
    async def back(self) -> bool: ...
    async def forward(self) -> bool: ...
    async def reload(self) -> dict[str, Any]: ...

    # -- observation -----------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        """Return {url, title, dom_hash, elements: [InteractiveElement-dicts]}."""

    async def find_links(self) -> list[dict[str, str]]: ...
    async def find_forms(self) -> list[dict[str, Any]]: ...
    async def detect_technologies(self) -> list[str]: ...

    # -- actions ---------------------------------------------------------
    async def click(self, ref: str) -> dict[str, Any]: ...
    async def fill(self, ref: str, value: str) -> dict[str, Any]: ...
    async def select_option(self, ref: str, value: str) -> dict[str, Any]: ...
    async def set_checked(self, ref: str, checked: bool) -> dict[str, Any]: ...
    async def hover(self, ref: str) -> dict[str, Any]: ...
    async def scroll(self, dy: int = 400) -> bool: ...
    async def press(self, key: str) -> bool: ...
    async def mouse_click(self, x: float, y: float) -> bool: ...

    # -- state -----------------------------------------------------------
    async def get_current_url(self) -> str: ...
    async def get_title(self) -> str: ...
    async def list_storage_keys(self, kind: str) -> list[str]: ...


def select_backend(settings: Settings) -> str:
    """Resolve the effective backend name from settings + environment.

    Returns "kernel" or "local". Kernel requires an explicit URL; auto
    only picks kernel when one is configured.
    """
    backend = settings.browser_backend
    if backend == "local":
        return "local"
    if backend == "kernel":
        if not settings.kernel_url:
            raise ValueError(
                "SBA_BROWSER_BACKEND=kernel requires SBA_KERNEL_URL to be set"
            )
        return "kernel"
    # auto: kernel only when configured.
    return "kernel" if settings.kernel_url else "local"


__all__ = ["BrowserRuntime", "select_backend"]
