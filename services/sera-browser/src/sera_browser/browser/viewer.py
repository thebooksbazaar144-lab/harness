"""Real-view browser viewer — human-takeoverable browser for SERA chat UI.

This module implements the "real view, not a relay" paradigm from dsh-browser:
the browser is visible (headed mode), the human can watch every step the agent
takes in real-time, and can grab control at any time.

Three layers:

1. **CDP remote debugging** — Chromium launches with `--remote-debugging-port`
   so the SERA UI (or `chrome://inspect`) can connect to the same browser
   instance for full interactive access.

2. **Screenshot + state streaming** — after each agent step, a screenshot +
   current URL/title/step info is streamed to connected WebSocket clients.
   The SERA chat UI renders this stream for real-time visibility.

3. **Human takeover** — the agent or the human can trigger a takeover. The
   agent pauses, the human interacts with the real browser window, then
   signals "done". The agent re-observes and continues.

Usage:
    viewer = BrowserViewer(task_id, session_dir)
    await viewer.start(page, cdp_port)
    # After each step:
    await viewer.broadcast_step(page, step_info)
    # Human takeover:
    await viewer.request_takeover("human wants to log in")
    await viewer.wait_for_resume(timeout_s=300)
    # On shutdown:
    await viewer.stop()

The SERA chat UI connects to `ws://host:port/tasks/{id}/stream` to receive
real-time screenshots + state, and calls `POST /tasks/{id}/human_takeover`
to pause the agent for manual interaction.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from ..cancel import CancellationManager, TaskCancelledError
from ..logging import get_logger

log = get_logger("sera.browser.viewer")


@dataclass
class StreamEvent:
    """One event sent to WebSocket clients."""
    type: str  # step | takeover | resume | screenshot | state
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {"type": self.type, "data": self.data, "timestamp": self.timestamp},
            ensure_ascii=False,
            default=str,
        )


@dataclass
class ViewerStats:
    """Stats for the viewer."""
    steps_streamed: int = 0
    screenshots_streamed: int = 0
    takeovers_requested: int = 0
    takeovers_completed: int = 0
    clients_connected: int = 0
    clients_current: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "steps_streamed": self.steps_streamed,
            "screenshots_streamed": self.screenshots_streamed,
            "takeovers_requested": self.takeovers_requested,
            "takeovers_completed": self.takeovers_completed,
            "clients_connected": self.clients_connected,
            "clients_current": self.clients_current,
        }


class BrowserViewer:
    """Manages real-time browser visibility + human takeover for one task.

    The viewer is created per task and tied to the task's primary page.
    It maintains a set of WebSocket clients (the SERA chat UI) that receive
    stream events.

    Human takeover flow:
    1. Agent (or human via API) calls `request_takeover(reason)`.
    2. The agent's main loop checks `is_takeover_active()` between steps.
    3. When active, the agent pauses and calls `wait_for_resume(timeout_s)`.
    4. The human interacts with the real browser window (visible, headed mode).
    5. The human (or API) calls `resume()`.
    6. The agent re-observes and continues.
    """

    def __init__(
        self,
        task_id: str,
        session_dir: Any,
        *,
        screenshot_quality: int = 60,
        screenshot_max_width: int = 1280,
        stream_screenshots: bool = True,
    ) -> None:
        self.task_id = task_id
        self.session_dir = session_dir
        self.screenshot_quality = screenshot_quality
        self.screenshot_max_width = screenshot_max_width
        self.stream_screenshots = stream_screenshots
        self._clients: set[asyncio.Queue[StreamEvent]] = set()
        self._takeover_active: bool = False
        self._takeover_reason: str = ""
        self._resume_event = asyncio.Event()
        self._page: Page | None = None
        self._cdp_port: int | None = None
        self.stats = ViewerStats()

    @property
    def cdp_url(self) -> str | None:
        """The CDP debugging URL for the browser (for chrome://inspect)."""
        if self._cdp_port is None:
            return None
        return f"http://127.0.0.1:{self._cdp_port}"

    @property
    def is_takeover_active(self) -> bool:
        return self._takeover_active

    def start(self, page: Page, cdp_port: int | None = None) -> None:
        """Attach the viewer to a page. Call after the page is created."""
        self._page = page
        self._cdp_port = cdp_port
        log.info(
            "viewer started",
            extra={
                "event": "viewer.start",
                "task_id": self.task_id,
                "cdp_port": cdp_port,
                "cdp_url": self.cdp_url,
            },
        )

    async def stop(self) -> None:
        """Shut down the viewer. Disconnects all clients."""
        # Send a close event to all clients.
        await self._broadcast(StreamEvent(type="close", data={"reason": "viewer stopped"}))
        self._clients.clear()
        self._page = None
        log.info(
            "viewer stopped",
            extra={"event": "viewer.stop", "task_id": self.task_id},
        )

    def add_client(self) -> asyncio.Queue[StreamEvent]:
        """Register a new WebSocket client. Returns a queue to read events from."""
        q: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=100)
        self._clients.add(q)
        self.stats.clients_connected += 1
        self.stats.clients_current = len(self._clients)
        log.info(
            "stream client connected",
            extra={
                "event": "viewer.client.connected",
                "task_id": self.task_id,
                "clients": len(self._clients),
            },
        )
        return q

    def remove_client(self, q: asyncio.Queue[StreamEvent]) -> None:
        """Unregister a WebSocket client."""
        self._clients.discard(q)
        self.stats.clients_current = len(self._clients)
        log.info(
            "stream client disconnected",
            extra={
                "event": "viewer.client.disconnected",
                "task_id": self.task_id,
                "clients": len(self._clients),
            },
        )

    async def broadcast_step(
        self,
        page: Page,
        *,
        step_num: int,
        action: str,
        result: str,
        detail: str = "",
    ) -> None:
        """Broadcast a step event (with optional screenshot) to all clients.

        Called by the BrowserAgent after each action.
        """
        # Capture state.
        url = page.url
        try:
            title = await page.title()
        except PlaywrightError:
            title = ""

        state_data: dict[str, Any] = {
            "step": step_num,
            "action": action,
            "result": result,
            "detail": detail[:200],
            "url": url,
            "title": title,
        }

        # Capture screenshot if enabled.
        if self.stream_screenshots:
            screenshot_b64 = await self._capture_screenshot_b64(page)
            if screenshot_b64:
                state_data["screenshot"] = screenshot_b64
                self.stats.screenshots_streamed += 1

        await self._broadcast(StreamEvent(type="step", data=state_data))
        self.stats.steps_streamed += 1

    async def request_takeover(self, reason: str = "human requested") -> None:
        """Request human takeover. The agent should call `wait_for_resume()`."""
        self._takeover_active = True
        self._takeover_reason = reason
        self._resume_event.clear()
        self.stats.takeovers_requested += 1
        log.info(
            "human takeover requested",
            extra={
                "event": "viewer.takeover.request",
                "task_id": self.task_id,
                "reason": reason,
            },
        )
        # Notify all clients.
        await self._broadcast(StreamEvent(
            type="takeover",
            data={
                "reason": reason,
                "message": "Agent paused. The human can now interact with the browser. "
                           "Call /tasks/{id}/resume to continue.",
            },
        ))

    async def wait_for_resume(
        self,
        timeout_s: float = 300.0,
        cancellation: CancellationManager | None = None,
    ) -> bool:
        """Wait for the human to signal resume. Returns True if resumed, False on timeout."""
        log.info(
            "waiting for human to finish (takeover)",
            extra={"event": "viewer.takeover.wait", "task_id": self.task_id, "timeout_s": timeout_s},
        )
        try:
            if cancellation is not None:
                await cancellation.wait_or_cancel(self._resume_event.wait(), timeout=timeout_s)
            else:
                await asyncio.wait_for(self._resume_event.wait(), timeout=timeout_s)
            return True
        except (asyncio.TimeoutError, TaskCancelledError):
            log.warning(
                "human takeover timed out",
                extra={"event": "viewer.takeover.timeout", "task_id": self.task_id, "timeout_s": timeout_s},
            )
            return False
        finally:
            self._takeover_active = False
            self.stats.takeovers_completed += 1
            await self._broadcast(StreamEvent(
                type="resume",
                data={"message": "Agent resuming after human takeover."},
            ))

    def resume(self) -> None:
        """Signal the agent to resume after human takeover."""
        self._takeover_active = False
        self._resume_event.set()
        log.info(
            "human signaled resume",
            extra={"event": "viewer.takeover.resume", "task_id": self.task_id},
        )

    async def capture_screenshot(self, page: Page) -> bytes | None:
        """Capture a screenshot as PNG bytes."""
        try:
            return await page.screenshot(
                type="png",
                full_page=False,
            )
        except PlaywrightError as e:
            log.warning("screenshot capture failed", extra={"event": "viewer.screenshot.fail", "err": str(e)})
            return None

    async def _capture_screenshot_b64(self, page: Page) -> str | None:
        """Capture a screenshot as a base64 data URL (JPEG for smaller size)."""
        try:
            png = await page.screenshot(type="jpeg", quality=self.screenshot_quality, full_page=False)
            return f"data:image/jpeg;base64,{base64.b64encode(png).decode('ascii')}"
        except PlaywrightError as e:
            log.debug("screenshot b64 failed", extra={"event": "viewer.screenshot.b64.fail", "err": str(e)})
            return None

    async def _broadcast(self, event: StreamEvent) -> None:
        """Send an event to all connected clients. Non-blocking; drops on full queue."""
        for q in list(self._clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, add newest.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


__all__ = ["BrowserViewer", "StreamEvent", "ViewerStats"]
