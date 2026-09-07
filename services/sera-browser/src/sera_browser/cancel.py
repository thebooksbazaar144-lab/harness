"""Cooperative cancellation.

A single `CancellationManager` per task. Every long operation periodically
checks `is_cancelled()` / raises `CancelledError` via `check()`.

Cancellation is **immediate** in the sense that any code path that respects
the token will abort at its next check. `force_cleanup` is a separate final
safety net in `BrowserManager`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TaskCancelledError(asyncio.CancelledError):
    """Raised when a task has been cancelled."""

    def __init__(self, task_id: str, reason: str = "cancelled") -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"task {task_id} {reason}")


@dataclass
class CancellationManager:
    task_id: str
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _reason: str = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def check(self) -> None:
        """Raise immediately if cancelled. Call from sync checkpoints."""
        if self._event.is_set():
            raise TaskCancelledError(self.task_id, self._reason)

    async def asleep(self, seconds: float) -> None:
        """asyncio.sleep that aborts immediately on cancel."""
        self.check()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=seconds)
            # If we got here, cancellation fired.
            raise TaskCancelledError(self.task_id, self._reason)
        except asyncio.TimeoutError:
            return

    async def wait_or_cancel(
        self, aw: Awaitable[T], *, timeout: float | None = None
    ) -> T:
        """Await a coroutine. If cancellation fires, cancel the awaitable
        and raise TaskCancelledError."""
        self.check()
        task = asyncio.ensure_future(aw)  # type: ignore[arg-type]
        try:
            if timeout is None:
                done, pending = await asyncio.wait(
                    {task, asyncio.ensure_future(self._event.wait())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                done, pending = await asyncio.wait(
                    {task, asyncio.ensure_future(self._event.wait())},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            for p in pending:
                p.cancel()
            if task in done:
                return task.result()
            if self._event.is_set():
                raise TaskCancelledError(self.task_id, self._reason)
            raise asyncio.TimeoutError()
        except TaskCancelledError:
            task.cancel()
            raise
        except asyncio.CancelledError:
            task.cancel()
            if self._event.is_set():
                raise TaskCancelledError(self.task_id, self._reason)
            raise


def cancellable(
    cm: CancellationManager, every_s: float = 0.2
) -> Callable[[Callable[[], bool]], None]:
    """Helper for long sync loops: call `cb()` until it returns True,
    checking cancellation every `every_s` seconds (asyncio-aware)."""

    async def _runner(cb: Callable[[], bool]) -> None:
        while not cb():
            await cm.asleep(every_s)

    return lambda cb: asyncio.create_task(_runner(cb))
