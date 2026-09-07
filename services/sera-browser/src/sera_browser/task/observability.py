"""Observability — Playwright traces + structured action log + OTel-ready spans.

Inspired by Playwright MCP's `start_tracing`/`start_video` and Stagehand's
OpenTelemetry integration. Provides:

1. **Playwright trace recording** — `.zip` traces that can be viewed in
   Trace Viewer (https://trace.playwright.dev/). Captures screenshots,
   network, DOM snapshots, and action timings.
2. **Structured action log** — a JSONL file recording every action with
   timestamp, args, result, duration, and snapshot version.
3. **OTel-ready spans** — lightweight span objects that can be exported to
   any OTel collector (Jaeger, Tempo, Honeycomb, etc.). We don't depend on
   the `opentelemetry` package; we emit spans as structured log events that
   an OTel collector can ingest via a log-to-span bridge.

The observability layer is opt-in per task (controlled by `SBA_TRACE_DIR`
env var or `trace` field in the task config).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from ..logging import get_logger

log = get_logger("sera.task.observability")


@dataclass
class ActionSpan:
    """A single action span for the structured action log."""
    span_id: str
    parent_id: str | None
    task_id: str
    name: str  # action name
    args: dict[str, Any]
    started_at: float
    finished_at: float | None = None
    duration_ms: float | None = None
    status: str = "running"  # running | ok | failed | cancelled
    detail: str = ""
    snapshot_version: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "task_id": self.task_id,
            "name": self.name,
            "args": self.args,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1) if self.duration_ms else None,
            "status": self.status,
            "detail": self.detail,
            "snapshot_version": self.snapshot_version,
            "attributes": self.attributes,
        }


class Observability:
    """Per-task observability: Playwright traces + structured action log.

    Usage:
        obs = Observability(task_id, session_dir)
        await obs.start_tracing(context)
        span = obs.start_span("navigate", {"url": "..."}, snapshot_version=1)
        ... do the action ...
        obs.finish_span(span, status="ok", detail="navigated")
        await obs.stop_tracing(context)
        obs.flush()
    """

    def __init__(
        self,
        task_id: str,
        session_dir: Path,
        *,
        enable_tracing: bool = True,
        enable_action_log: bool = True,
    ) -> None:
        self.task_id = task_id
        self.session_dir = session_dir
        self.enable_tracing = enable_tracing
        self.enable_action_log = enable_action_log
        self._spans: list[ActionSpan] = []
        self._span_counter = 0
        self._trace_path: Path | None = None
        self._action_log_path: Path | None = None
        self._tracing_started = False

    async def start_tracing(self, context: BrowserContext) -> None:
        """Start Playwright trace recording on the context."""
        if not self.enable_tracing:
            return
        self._trace_path = self.session_dir / "trace.zip"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            await context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=True,
                title=f"SERA task {self.task_id}",
            )
            self._tracing_started = True
            log.info(
                "playwright tracing started",
                extra={"event": "trace.start", "task_id": self.task_id},
            )
        except PlaywrightError as e:
            log.warning(
                "playwright tracing failed to start",
                extra={"event": "trace.start.fail", "err": str(e)},
            )
            self.enable_tracing = False

    async def stop_tracing(self, context: BrowserContext) -> None:
        """Stop Playwright trace recording and save to disk."""
        if not self._tracing_started:
            return
        try:
            await context.tracing.stop(path=str(self._trace_path))
            log.info(
                "playwright tracing saved",
                extra={"event": "trace.stop", "path": str(self._trace_path), "task_id": self.task_id},
            )
        except PlaywrightError as e:
            log.warning(
                "playwright tracing failed to stop",
                extra={"event": "trace.stop.fail", "err": str(e)},
            )
        finally:
            self._tracing_started = False

    def start_span(
        self,
        name: str,
        args: dict[str, Any],
        *,
        parent_id: str | None = None,
        snapshot_version: int | None = None,
    ) -> ActionSpan:
        """Start a new action span. Returns the span for later finishing."""
        self._span_counter += 1
        span_id = f"{self.task_id}-{self._span_counter}"
        span = ActionSpan(
            span_id=span_id,
            parent_id=parent_id,
            task_id=self.task_id,
            name=name,
            args=dict(args),
            started_at=time.time(),
            snapshot_version=snapshot_version,
        )
        self._spans.append(span)
        return span

    def finish_span(
        self,
        span: ActionSpan,
        *,
        status: str = "ok",
        detail: str = "",
    ) -> None:
        """Finish an action span."""
        span.finished_at = time.time()
        span.duration_ms = (span.finished_at - span.started_at) * 1000.0
        span.status = status
        span.detail = detail
        # Emit as a structured log event (OTel collector can ingest).
        # NOTE: 'name' and 'msg' are reserved LogRecord attributes — use
        # 'action_name' instead to avoid KeyError in Python's logging.
        log.info(
            f"action span: {span.name}",
            extra={
                "event": "span.finish",
                "span_id": span.span_id,
                "task_id": span.task_id,
                "action_name": span.name,
                "duration_ms": round(span.duration_ms, 1),
                "status": span.status,
                "detail": span.detail[:200],
                "snapshot_version": span.snapshot_version,
            },
        )

    def flush(self) -> None:
        """Write all spans to the structured action log (JSONL)."""
        if not self.enable_action_log:
            return
        self._action_log_path = self.session_dir / "actions.jsonl"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._action_log_path, "w", encoding="utf-8") as f:
                for span in self._spans:
                    f.write(json.dumps(span.to_dict(), ensure_ascii=False, default=str) + "\n")
            log.info(
                "action log flushed",
                extra={
                    "event": "action_log.flush",
                    "path": str(self._action_log_path),
                    "span_count": len(self._spans),
                },
            )
        except OSError as e:
            log.warning("action log flush failed", extra={"event": "action_log.fail", "err": str(e)})

    @property
    def spans(self) -> list[ActionSpan]:
        return list(self._spans)

    @property
    def trace_path(self) -> Path | None:
        return self._trace_path

    @property
    def action_log_path(self) -> Path | None:
        return self._action_log_path

    def summary(self) -> dict[str, Any]:
        """A summary of observability for the final report."""
        total = len(self._spans)
        ok = sum(1 for s in self._spans if s.status == "ok")
        failed = sum(1 for s in self._spans if s.status == "failed")
        cancelled = sum(1 for s in self._spans if s.status == "cancelled")
        durations = [s.duration_ms for s in self._spans if s.duration_ms]
        return {
            "total_spans": total,
            "ok": ok,
            "failed": failed,
            "cancelled": cancelled,
            "total_duration_ms": round(sum(durations), 1) if durations else 0,
            "mean_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "trace_path": str(self._trace_path) if self._trace_path else None,
            "action_log_path": str(self._action_log_path) if self._action_log_path else None,
        }


__all__ = ["Observability", "ActionSpan"]
