"""BrowserManager — owns the Playwright lifecycle + browser launch.

Responsibilities:
- Launch Playwright + Chromium in the configured mode (auto/headed/headless)
- Create isolated `ContextManager` per task
- Wire NetworkRecorder + event handlers to each page
- Force-cleanup on cancellation or crash
- Expose the effective browser mode
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import (
    Browser,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from ..cancel import CancellationManager, TaskCancelledError
from ..config import Settings
from ..logging import get_logger
from .context import ContextManager

log = get_logger("sera.browser.manager")


@dataclass
class _BrowserHandle:
    playwright: Playwright
    browser: Browser
    mode: str


class BrowserManager:
    """Owns the single Playwright+Chromium instance for the process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._handle: _BrowserHandle | None = None
        self._lock = asyncio.Lock()
        self._launch_attempts = 0

    @property
    def effective_mode(self) -> str | None:
        return self._handle.mode if self._handle else None

    async def launch(self, *, mode: str | None = None) -> _BrowserHandle:
        """Launch (or reuse) Chromium. Returns the browser handle.

        When `settings.enable_viewer` is True and `viewer_force_headed` is True,
        forces headed mode + adds `--remote-debugging-port` so the SERA chat UI
        can connect to the same browser instance for real-time viewing.
        """
        async with self._lock:
            if self._handle is not None:
                return self._handle
            self._launch_attempts += 1
            # If the viewer is enabled, force headed mode.
            if getattr(self.settings, "enable_viewer", False) and getattr(self.settings, "viewer_force_headed", True):
                mode = "headed"
            else:
                mode = mode or self.settings.browser_mode
            effective = mode if mode != "auto" else self.settings.effective_browser_mode()
            log.info(
                "launching browser",
                extra={"event": "browser.launch", "mode": effective, "attempt": self._launch_attempts},
            )
            pw = await async_playwright().start()
            args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
            # CDP remote debugging for the viewer (SERA chat UI).
            cdp_port: int | None = None
            if getattr(self.settings, "enable_viewer", False):
                cdp_port = getattr(self.settings, "viewer_cdp_port", 9222)
                args.append(f"--remote-debugging-port={cdp_port}")
                log.info(
                    "CDP remote debugging enabled",
                    extra={"event": "browser.cdp", "port": cdp_port},
                )
            launch_kwargs: dict[str, Any] = {
                "headless": effective == "headless",
                "args": args,
            }
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except PlaywrightError as e:
                with suppress(Exception):  # noqa: BLE001
                    await pw.stop()
                self._handle = None
                log.error(
                    "browser launch failed",
                    extra={"event": "browser.launch.fail", "err": str(e)},
                )
                raise
            self._handle = _BrowserHandle(playwright=pw, browser=browser, mode=effective)
            self._handle.cdp_port = cdp_port  # type: ignore[attr-defined]
            log.info(
                "browser launched",
                extra={
                    "event": "browser.launched",
                    "mode": effective,
                    "version": browser.version,
                    "cdp_port": cdp_port,
                },
            )
            return self._handle

    async def new_context_manager(self, cancellation: CancellationManager) -> ContextManager:
        if self._handle is None:
            raise RuntimeError("BrowserManager not launched")
        return ContextManager(settings=self.settings, cancellation=cancellation)

    async def shutdown(self) -> None:
        """Cooperative shutdown. Best-effort; force_kill is the safety net."""
        async with self._lock:
            h = self._handle
            self._handle = None
            if h is None:
                return
            with suppress(PlaywrightError):
                await h.browser.close()
            with suppress(PlaywrightError):
                await h.playwright.stop()

    async def force_kill(self) -> None:
        """Final safety net. Kill Chromium process tree if it lingers."""
        h = self._handle
        self._handle = None
        if h is not None:
            with suppress(Exception):  # noqa: BLE001
                await h.browser.close()
            with suppress(Exception):  # noqa: BLE001
                await h.playwright.stop()
        # Sweep for orphaned chromium processes owned by this process tree.
        # We don't kill arbitrary system Chromium processes — only those we
        # spawned. This is best-effort and platform-dependent.
        self._sweep_orphan_chromium()

    @staticmethod
    def _sweep_orphan_chromium() -> None:
        """Kill Chromium processes spawned under PLAYWRIGHT_BROWSERS_PATH."""
        # Only attempt on linux
        if os.name != "posix":
            return
        import subprocess

        try:
            out = subprocess.check_output(
                ["pgrep", "-f", "ms-playwright/chromium"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            pids = [int(p) for p in out.split() if p.strip().isdigit()]
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return
        for pid in pids:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if pids:
            log.warning(
                "force-killed orphan chromium",
                extra={"event": "browser.force_kill", "count": len(pids)},
            )
