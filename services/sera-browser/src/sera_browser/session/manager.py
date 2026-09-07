"""SessionManager — owns tasks, ids, lifecycle, isolation.

- create → use → cancel → close → cleanup
- Per-task isolated context
- Two simultaneous sessions are supported
- One task cannot control another task's browser
- `cancel_task(task_id)` propagates through the whole stack
- `force_cleanup` runs before a cancelled task is marked finished
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..browser.manager import BrowserManager
from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger
from ..models import AgentReport, BrowserState, TaskCreate
from ..security import validate_target
from ..task.agent import BrowserAgent, TaskConfig, TaskResult

log = get_logger("sera.session")


class SessionStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"
    blocked = "blocked"
    timeout = "timeout"


@dataclass
class SessionRecord:
    task_id: str
    config: TaskConfig
    status: SessionStatus = SessionStatus.pending
    state: BrowserState | None = None
    report: AgentReport | None = None
    error: str | None = None
    cancellation: CancellationManager | None = None
    task: asyncio.Task | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    # Real-view browser viewer (set by the agent when enable_viewer=True).
    viewer: Any = None

    @property
    def viewer_enabled(self) -> bool:
        return self.viewer is not None


class SessionManager:
    def __init__(self, settings: Settings, browser_manager: BrowserManager) -> None:
        self.settings = settings
        self.browser_manager = browser_manager
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: TaskCreate) -> SessionRecord:
        async with self._lock:
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            cfg = TaskConfig(
                task_id=task_id,
                target=request.target,
                objective=request.task,
                browser_mode=request.browser_mode,
                timeout_s=request.timeout or self.settings.max_task_duration_s,
                local_lab=request.local_lab,
                allowed_targets=request.allowed_targets,
                allowed_domains=request.allowed_domains,
                auth=request.auth,
                max_steps=request.max_steps,
                allow_js_eval=request.allow_js_eval,
            )
            cancellation = CancellationManager(task_id=task_id)
            record = SessionRecord(task_id=task_id, config=cfg, cancellation=cancellation)
            self._sessions[task_id] = record

        # Validate target eagerly so a bad target returns immediately.
        try:
            # Re-apply overrides for validation only (mirrors BrowserAgent logic).
            s = self.settings.model_copy(deep=True)
            if cfg.local_lab:
                s.local_lab = True
                existing = set(s.allowed_private_targets_list)
                existing.update(cfg.allowed_targets)
                s.allowed_private_targets = ",".join(sorted(existing))
            if cfg.allowed_domains:
                existing = set(s.allowed_domains_list)
                existing.update(cfg.allowed_domains)
                s.allowed_domains = ",".join(sorted(existing))
            validate_target(cfg.target, s)
        except PermissionError as e:
            record.status = SessionStatus.failed
            record.error = f"target rejected: {e}"
            record.finished_at = asyncio.get_event_loop().time()
            return record

        # Run the agent in the background.
        agent = BrowserAgent(
            settings=self.settings,
            browser_manager=self.browser_manager,
            on_state=lambda s: self._attach_state(task_id, s),
        )
        # Wire viewer onto the session record after the agent starts.
        # The agent sets self._viewer when it creates the viewer; we poll
        # for it in the runner below.
        record.status = SessionStatus.running
        record.started_at = asyncio.get_event_loop().time()

        async def _runner() -> None:
            try:
                result: TaskResult = await agent.run(cfg, cancellation)
                record.report = result.report
                record.error = result.error
                if result.report is not None:
                    record.status = SessionStatus(result.report.status)
                else:
                    record.status = SessionStatus.failed
            except Exception as e:  # noqa: BLE001
                record.status = SessionStatus.failed
                record.error = repr(e)
                log.exception("session runner crashed", extra={"event": "session.crash", "task_id": task_id})
            finally:
                record.finished_at = asyncio.get_event_loop().time()
                record.task = None

        # Wire the viewer reference onto the record (best-effort; the agent
        # creates it during run(), so we pass it via a closure).
        def _attach_viewer():
            v = getattr(agent, "_viewer", None)
            if v is not None:
                record.viewer = v
        record._attach_viewer = _attach_viewer  # type: ignore[attr-defined]

        record.task = asyncio.create_task(_runner(), name=f"sera:{task_id}")
        return record

    def get(self, task_id: str) -> SessionRecord | None:
        return self._sessions.get(task_id)

    def list(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    def _attach_state(self, task_id: str, state: BrowserState) -> None:
        """Used by the agent to expose live BrowserState for polling."""
        rec = self._sessions.get(task_id)
        if rec is not None:
            rec.state = state
            # Also try to attach the viewer reference.
            attach_fn = getattr(rec, "_attach_viewer", None)
            if attach_fn is not None:
                attach_fn()

    async def request_takeover(self, task_id: str, reason: str = "human requested") -> bool:
        """Request human takeover for a task with an active viewer.

        Returns True if the takeover was requested (or already active),
        False if the task has no viewer or is not running.
        """
        rec = self._sessions.get(task_id)
        if rec is None or rec.viewer is None:
            return False
        if rec.viewer.is_takeover_active:
            return True  # already active
        await rec.viewer.request_takeover(reason)
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a task after human takeover.

        Returns True if the resume was signaled, False if the task has no
        viewer or no active takeover.
        """
        rec = self._sessions.get(task_id)
        if rec is None or rec.viewer is None:
            return False
        if not rec.viewer.is_takeover_active:
            return False
        rec.viewer.resume()
        return True

    async def cancel(self, task_id: str, reason: str = "user-requested") -> bool:
        """Cooperative cancellation. Returns True if a task was cancelled."""
        rec = self._sessions.get(task_id)
        if rec is None:
            return False
        cm = rec.cancellation
        if cm is None:
            # Task already finished or never had a token.
            if rec.task is not None and not rec.task.done():
                rec.task.cancel()
            return False
        cm.cancel(reason=reason)
        # Give the task a moment to clean up.
        if rec.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(rec.task), timeout=15.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return True

    async def shutdown_all(self) -> None:
        """Cancel everything and force-cleanup. Used on API shutdown."""
        for tid in list(self._sessions.keys()):
            await self.cancel(tid, reason="shutdown")
        await self.browser_manager.force_kill()
