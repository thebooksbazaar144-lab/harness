"""Browser subsystem: PlaywrightDriver, BrowserManager, ContextManager,
Inspector, ScreenshotLayer, StorageManager, optional BrowserUseFallback."""
from __future__ import annotations

from .driver import PlaywrightDriver
from .manager import BrowserManager
from .context import ContextManager
from .inspector import Inspector
from .screenshots import ScreenshotLayer
from .storage import StorageManager

__all__ = [
    "PlaywrightDriver",
    "BrowserManager",
    "ContextManager",
    "Inspector",
    "ScreenshotLayer",
    "StorageManager",
]
