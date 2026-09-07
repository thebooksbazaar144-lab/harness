"""FastAPI HTTP API.

Endpoints:
    POST /tasks                  create + start a browser agent task
    GET  /tasks/{id}             task status + metadata
    POST /tasks/{id}/cancel      cancel a running task
    GET  /tasks/{id}/state       current BrowserState snapshot
    GET  /tasks/{id}/result      final structured report
    GET  /health                 liveness + effective browser mode

Real-view browser viewer (human-takeoverable, for SERA chat UI):
    GET  /tasks/{id}/viewer       viewer status (CDP URL, takeover state)
    POST /tasks/{id}/human_takeover  pause agent for human interaction
    POST /tasks/{id}/resume       resume agent after human takeover
    GET  /tasks/{id}/screenshot   current page screenshot (base64 data URL)
    WS   /tasks/{id}/stream       real-time browser state stream (screenshots + steps)
"""
from __future__ import annotations

import asyncio
from importlib import metadata as _md
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..browser.manager import BrowserManager

try:  # avoid a circular import with the package __init__
    __version__ = _md.version("sera-browser")
except Exception:  # noqa: BLE001
    __version__ = "3.1.1"
from ..config import Settings, get_settings
from ..logging import configure_logging, get_logger
from ..models import TaskCreate, TaskCreatedResponse
from ..session.manager import SessionManager, SessionStatus

log = get_logger("sera.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    browser_manager = BrowserManager(settings=settings)
    sessions = SessionManager(settings=settings, browser_manager=browser_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        try:
            yield
        finally:
            log.info("shutting down: cancelling all sessions + force-cleanup")
            try:
                await asyncio.wait_for(sessions.shutdown_all(), timeout=20.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                await browser_manager.force_kill()

    app = FastAPI(
        title="SERA Browser Agent",
        version=__version__,
        description="Playwright-first autonomous browser agent.",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "browser_mode": settings.effective_browser_mode(),
            "browser_launched": browser_manager._handle is not None,
            "browser_use_available": _browser_use_available(),
            "viewer_enabled": settings.enable_viewer,
            "version": __version__,
        }

    @app.post("/tasks", response_model=TaskCreatedResponse, status_code=202)
    async def create_task(req: TaskCreate) -> TaskCreatedResponse:
        rec = await sessions.create(req)
        if rec.status == SessionStatus.failed:
            raise HTTPException(status_code=400, detail=rec.error or "target rejected")
        return TaskCreatedResponse(
            task_id=rec.task_id,
            status=rec.status.value,
            target=rec.config.target,
        )

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task_id": rec.task_id,
            "target": rec.config.target,
            "task": rec.config.objective,
            "browser_mode": rec.config.browser_mode,
            "status": rec.status.value,
            "error": rec.error,
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
            "has_state": rec.state is not None,
            "has_report": rec.report is not None,
        }

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        cancelled = await sessions.cancel(task_id)
        return {
            "task_id": task_id,
            "cancelled": cancelled,
            "status": rec.status.value,
        }

    @app.get("/tasks/{task_id}/state")
    async def get_state(task_id: str, with_snapshot: bool = False) -> dict[str, Any]:
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.state is None:
            return {"task_id": task_id, "state": None}
        return {
            "task_id": task_id,
            "state": rec.state.to_api_dict(with_snapshot=with_snapshot),
        }

    @app.get("/tasks/{task_id}/result")
    async def get_result(task_id: str) -> dict[str, Any]:
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.report is None:
            raise HTTPException(status_code=409, detail="report not ready")
        return rec.report.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Real-view browser viewer (human-takeoverable browser for SERA chat UI)
    # ------------------------------------------------------------------

    @app.get("/tasks/{task_id}/viewer")
    async def get_viewer_status(task_id: str) -> dict[str, Any]:
        """Get the viewer status for a task (CDP URL, takeover state, stats)."""
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.viewer is None:
            return {"task_id": task_id, "viewer_enabled": False}
        return {
            "task_id": task_id,
            "viewer_enabled": True,
            "cdp_url": rec.viewer.cdp_url,
            "cdp_port": rec.viewer._cdp_port,
            "takeover_active": rec.viewer.is_takeover_active,
            "takeover_reason": rec.viewer._takeover_reason if rec.viewer.is_takeover_active else None,
            "stats": rec.viewer.stats.as_dict(),
        }

    @app.post("/tasks/{task_id}/human_takeover")
    async def human_takeover(task_id: str, reason: str = "human requested") -> dict[str, Any]:
        """Request human takeover — pauses the agent so the human can interact
        with the visible browser window. The agent resumes when /resume is called.
        """
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.viewer is None:
            raise HTTPException(status_code=400, detail="viewer not enabled for this task")
        ok = await sessions.request_takeover(task_id, reason)
        return {
            "task_id": task_id,
            "takeover_active": ok,
            "message": "Agent paused. Interact with the browser window, then call /resume.",
        }

    @app.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        """Resume the agent after human takeover."""
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.viewer is None:
            raise HTTPException(status_code=400, detail="viewer not enabled for this task")
        ok = sessions.resume_task(task_id)
        return {
            "task_id": task_id,
            "resumed": ok,
            "message": "Agent resuming." if ok else "No active takeover to resume.",
        }

    @app.get("/tasks/{task_id}/screenshot")
    async def get_screenshot(task_id: str) -> dict[str, Any]:
        """Capture and return the current page screenshot as a base64 data URL."""
        rec = sessions.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.viewer is None or rec.viewer._page is None:
            raise HTTPException(status_code=400, detail="viewer/page not available")
        png = await rec.viewer.capture_screenshot(rec.viewer._page)
        if png is None:
            raise HTTPException(status_code=500, detail="screenshot capture failed")
        import base64

        data_url = f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
        return {"task_id": task_id, "screenshot": data_url}

    @app.websocket("/tasks/{task_id}/stream")
    async def stream_task(websocket: Any) -> None:
        """WebSocket endpoint for real-time browser state streaming.

        Sends StreamEvent JSON objects (step, takeover, resume, close) to
        connected clients. The SERA chat UI connects here to receive
        real-time screenshots + state after each agent step.
        """
        from fastapi import WebSocket as FastAPIWebSocket

        task_id = websocket.path_params.get("task_id", "")
        rec = sessions.get(task_id)
        if rec is None:
            await websocket.close(code=4004, reason="task not found")
            return
        if rec.viewer is None:
            await websocket.close(code=4003, reason="viewer not enabled")
            return
        await websocket.accept()
        q = rec.viewer.add_client()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    await websocket.send_text(event.to_json())
                except asyncio.TimeoutError:
                    # Send a keepalive ping.
                    await websocket.send_json({"type": "ping", "timestamp": time.time()})
        except Exception:  # noqa: BLE001
            pass
        finally:
            rec.viewer.remove_client(q)

    @app.get("/tasks")
    async def list_tasks() -> list[dict[str, Any]]:
        return [
            {
                "task_id": r.task_id,
                "target": r.config.target,
                "status": r.status.value,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "has_report": r.report is not None,
            }
            for r in sessions.list()
        ]

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": "SERA Browser Agent", "version": __version__}

    return app


def _browser_use_available() -> bool:
    try:
        from ..browser.browser_use_fallback import browser_use_available as _impl  # type: ignore

        return _impl()
    except Exception:  # noqa: BLE001
        return False


# ASGI entry point
app = create_app()
