"""Optional browser-use fallback driver.

- Lazy-imported. If `browser-use` is not installed, instantiation fails with
  a clear message and the agent falls back to declaring the task blocked.
- Shares the same BrowserState, SessionManager, CancellationManager,
  NetworkRecorder, and result format. It does NOT spawn a parallel browser
  lifecycle.
- Never the normal execution path. Invoked only when:
    1. Playwright interaction cannot complete the task (after retries),
    2. `Settings.enable_browser_use_fallback` is True, AND
    3. `browser-use` is importable.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger
from ..models import BrowserState

log = get_logger("sera.browser.browser_use_fallback")


def browser_use_available() -> bool:
    try:
        import browser_use  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class BrowserUseFallback:
    """Lazy wrapper around browser-use.

    The wrapper deliberately exposes a tiny surface so the agent never
    depends on browser-use's internals. It receives the same BrowserState
    and CancellationManager as the Playwright path, mutates state in place,
    and returns nothing — the agent reads state afterwards.
    """

    def __init__(self, settings: Settings, cancellation: CancellationManager) -> None:
        if not settings.enable_browser_use_fallback:
            raise PermissionError("browser-use fallback disabled by settings")
        if not browser_use_available():
            raise ImportError(
                "browser-use is not installed. Install with "
                "`pip install \".[fallback]\"` to enable the fallback driver."
            )
        self.settings = settings
        self.cancellation = cancellation

    async def attempt(
        self,
        state: BrowserState,
        objective: str,
        *,
        max_steps: int = 10,
    ) -> bool:
        """Attempt the objective via browser-use. Returns True on success.

        This is a thin integration: we hand the objective to browser-use
        and let it drive. Network events from the page are already being
        captured by NetworkRecorder wired in the Playwright path.

        NOTE: browser-use may launch its own browser context. For maximum
        isolation we accept this and ensure the CancellationManager cancels
        its tasks too.
        """
        # Lazy import inside the method so the module imports cleanly when
        # browser-use is absent.
        try:
            from browser_use import Agent as BUA  # type: ignore
            from browser_use import Browser as BUB  # type: ignore
        except Exception as e:  # noqa: BLE001
            log.error(
                "browser-use import failed",
                extra={"event": "browser_use.import.fail", "err": str(e)},
            )
            return False

        # We don't want to spawn a parallel browser lifecycle indefinitely.
        # Bound it strictly to `max_steps`.
        self.cancellation.check()
        try:
            browser = BUB()
            agent = BUA(
                task=objective,
                browser=browser,
                max_steps=max_steps,
            )
            # Run it under a cancellation-aware wait so cancel propagates.
            await self.cancellation.wait_or_cancel(agent.run())  # type: ignore[arg-type]
            return True
        except Exception as e:  # noqa: BLE001
            log.warning(
                "browser-use attempt failed",
                extra={"event": "browser_use.fail", "err": str(e)},
            )
            return False
        finally:
            # Best-effort cleanup of browser-use's own context.
            try:
                if 'browser' in locals():
                    await asyncio.wait_for(browser.close(), timeout=5.0)  # type: ignore[union-attr]
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
