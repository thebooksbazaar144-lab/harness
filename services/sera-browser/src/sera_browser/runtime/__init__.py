"""Runtime package: BrowserRuntime backend selection.

```
BrowserRuntime
├── LocalPlaywrightRuntime   (fully supported agent backend)
└── KernelRuntime            (remote kernel browser service)
```

``create_runtime`` resolves ``SBA_BROWSER_BACKEND`` (auto|local|kernel):
- ``local`` → LocalPlaywrightRuntime (wraps the live driver/page/registry)
- ``kernel`` → KernelRuntime (must have SBA_KERNEL_URL)
- ``auto``  → kernel when a healthy SBA_KERNEL_URL is configured, else
              local. The kernel probe is ONE bounded request; a failure
              degrades to local without retry loops.
"""
from __future__ import annotations

from typing import Any

from ..config import Settings
from ..logging import get_logger
from .base import BrowserRuntime, select_backend
from .kernel import KernelRuntime
from .local import LocalPlaywrightRuntime

log = get_logger("sera.runtime")

__all__ = [
    "BrowserRuntime",
    "LocalPlaywrightRuntime",
    "KernelRuntime",
    "create_runtime",
    "select_backend",
]


async def create_runtime(
    settings: Settings,
    *,
    task_id: str,
    driver: Any = None,
    page: Any = None,
    registry: Any = None,
) -> tuple[BrowserRuntime, str]:
    """Build the runtime selected by settings.

    Returns (runtime, backend_name). For ``local`` the caller supplies the
    live driver/page/registry; for ``kernel`` a session is started (or the
    local runtime is returned as a degraded fallback under ``auto``).
    """
    backend = select_backend(settings)
    if backend == "kernel":
        kernel = KernelRuntime(settings, task_id=task_id)
        if await kernel.health() and await kernel.start():
            return kernel, "kernel"
        await kernel.stop()
        if settings.browser_backend == "kernel":
            raise RuntimeError(
                "SBA_BROWSER_BACKEND=kernel but the kernel service is unreachable: "
                f"{settings.kernel_url}"
            )
        log.warning(
            "kernel backend unhealthy; auto-falling back to local",
            extra={"event": "runtime.kernel.fallback", "url": settings.kernel_url},
        )
        backend = "local"
    # local (default or auto fallback)
    if driver is None or page is None:
        raise ValueError("local runtime requires driver and page")
    if registry is None:
        from ..browser.element_registry import ElementRegistry

        registry = registry or ElementRegistry()
    return LocalPlaywrightRuntime(settings, driver, page, registry), "local"
