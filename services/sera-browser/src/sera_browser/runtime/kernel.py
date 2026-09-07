"""KernelRuntime — remote kernel browser-service backend (rebuild v4).

WIRE CONTRACT (kernel browser service, v1)
==========================================

The kernel service exposes a session-scoped REST API. All responses are
JSON. Every response carries ``{"ok": bool, ...}``; failures carry
``{"ok": false, "error": {"kind": str, "detail": str}}`` where ``kind``
uses the same failure vocabulary as ``task/failure.py``.

Base: ``SBA_KERNEL_URL`` (e.g. ``http://127.0.0.1:9223``)

    GET  /health
        -> {"ok": true, "service": "kernel-browser", "version": "..."}

    POST /sessions                     {"task_id": str}
        -> {"ok": true, "session_id": str}
    DELETE /sessions/{sid}
        -> {"ok": true}

    POST /sessions/{sid}/navigate      {"url": str, "wait_until"?: str}
        -> {"ok": true, "url": str, "ms": float}
    POST /sessions/{sid}/back          {}
    POST /sessions/{sid}/forward       {}
    POST /sessions/{sid}/reload        {}

    GET  /sessions/{sid}/snapshot
        -> {"ok": true, "url": str, "title": str, "dom_hash": str,
            "elements": [
                {"ref": "e001", "role": "link", "name": "Home",
                 "tag": "a", "href": "/",
                 "locator": {"type": "role", "role": "link", "name": "Home"},
                 ...}
            ]}
        Refs are kernel-side registry refs; the caller maps them into its
        own ElementRegistry namespace via content signatures.

    POST /sessions/{sid}/actions       {"action": "click", "ref": "e001"}
        actions: click | fill | select_option | set_checked | hover
        fill: {"action": "fill", "ref": "...", "value": "..."}
        select_option: {..., "value": "..."}
        set_checked: {..., "checked": bool}
        -> {"ok": true} | {"ok": false, "error": {...}}

    POST /sessions/{sid}/scroll        {"dy": int}
    POST /sessions/{sid}/press         {"key": str}
    POST /sessions/{sid}/mouse_click   {"x": num, "y": num}

    GET  /sessions/{sid}/links         -> {"ok": true, "links": [{href, text}]}
    GET  /sessions/{sid}/forms         -> {"ok": true, "forms": [...]}
    GET  /sessions/{sid}/technologies  -> {"ok": true, "technologies": [str]}
    GET  /sessions/{sid}/storage       {"kind": "localStorage"}
        -> {"ok": true, "keys": [str]}

Design rules honored here:
- ONE bounded budget per call (``kernel_timeout_s``), no sequential
  fallback paths (the v3 30-second-click lesson applies to HTTP too).
- Provider errors are classified, never retried into loops.
- ``auto`` backend selection probes /health once; unhealthy ⇒ local.
- The kernel stays INFRASTRUCTURE: no application logic lives here
  (decision: Kernel is a runtime backend, not a prerequisite).
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from ..logging import get_logger
from ..models import InteractiveElement
from ..task.failure import ActionFailure, FailureKind

log = get_logger("sera.runtime.kernel")


class KernelRuntime:
    """BrowserRuntime implemented over the kernel browser-service REST API."""

    name = "kernel"

    def __init__(self, settings: Settings, *, task_id: str, session_id: str | None = None) -> None:
        self.settings = settings
        self.task_id = task_id
        self._base = settings.kernel_url.rstrip("/")
        self._session_id = session_id
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=settings.kernel_timeout_s,
        )
        # Ref translation: local registry ref -> kernel ref.
        self._ref_map: dict[str, str] = {}
        self._reverse_ref_map: dict[str, str] = {}
        self._last_elements: dict[str, InteractiveElement] = {}

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One bounded request. Errors come back as classified failures —
        never retried here, never looped."""
        try:
            resp = await self._client.request(method, path, json=json_body)
        except httpx.TimeoutException as e:
            return {"ok": False, "error": {"kind": "timeout", "detail": str(e)[:200]}}
        except httpx.ConnectError as e:
            return {"ok": False, "error": {"kind": "browser_disconnected", "detail": str(e)[:200]}}
        except httpx.HTTPError as e:
            return {"ok": False, "error": {"kind": "exception", "detail": str(e)[:200]}}
        if resp.status_code >= 500:
            return {
                "ok": False,
                "error": {"kind": "provider_error", "detail": f"HTTP {resp.status_code}"},
            }
        if resp.status_code == 404:
            return {
                "ok": False,
                "error": {"kind": "unknown_ref", "detail": f"HTTP 404 {path}"},
            }
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "error": {"kind": "provider_error", "detail": "invalid JSON"}}

    def _sid(self) -> str:
        if self._session_id is None:
            raise RuntimeError("kernel session not started — call start()")
        return self._session_id

    def _fail(self, kind: str, detail: str) -> dict[str, Any]:
        return {"ok": False, "failure": ActionFailure(kind=kind, detail=detail)}  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Create the kernel session. Returns ok."""
        if self._session_id is not None:
            return True
        res = await self._request("POST", "/sessions", json_body={"task_id": self.task_id})
        if res.get("ok") and res.get("session_id"):
            self._session_id = str(res["session_id"])
            log.info(
                "kernel session started",
                extra={"event": "kernel.session.start", "session": self._session_id},
            )
            return True
        err = res.get("error", {})
        log.warning(
            "kernel session start failed",
            extra={"event": "kernel.session.fail", "kind": err.get("kind"), "detail": err.get("detail")},
        )
        return False

    async def stop(self) -> None:
        if self._session_id is not None:
            await self._request("DELETE", f"/sessions/{self._session_id}")
            self._session_id = None
        await self._client.aclose()

    async def health(self) -> bool:
        res = await self._request("GET", "/health")
        return bool(res.get("ok"))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        res = await self._request(
            "POST", f"/sessions/{self._sid()}/navigate", json_body={"url": url}
        )
        if not res.get("ok"):
            err = res.get("error", {})
            return self._fail(err.get("kind", "exception"), err.get("detail", ""))
        return {"ok": True, "url": res.get("url", url), "ms": res.get("ms", 0.0)}

    async def back(self) -> bool:
        res = await self._request("POST", f"/sessions/{self._sid()}/back", json_body={})
        return bool(res.get("ok"))

    async def forward(self) -> bool:
        res = await self._request("POST", f"/sessions/{self._sid()}/forward", json_body={})
        return bool(res.get("ok"))

    async def reload(self) -> dict[str, Any]:
        res = await self._request("POST", f"/sessions/{self._sid()}/reload", json_body={})
        if not res.get("ok"):
            err = res.get("error", {})
            return self._fail(err.get("kind", "exception"), err.get("detail", ""))
        return {"ok": True, "url": res.get("url", ""), "ms": res.get("ms", 0.0)}

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        res = await self._request("GET", f"/sessions/{self._sid()}/snapshot")
        if not res.get("ok"):
            err = res.get("error", {})
            return {"ok": False, "error": err}
        elements: list[InteractiveElement] = []
        for e in res.get("elements", []):
            try:
                el = InteractiveElement(
                    ref=e.get("ref"),
                    role=e.get("role", "generic"),
                    name=e.get("name", ""),
                    tag=e.get("tag"),
                    href=e.get("href"),
                    type=e.get("type"),
                    placeholder=e.get("placeholder"),
                    disabled=bool(e.get("disabled", False)),
                )
            except Exception:  # noqa: BLE001 — one bad element must not kill the snapshot
                continue
            elements.append(el)
            kref = e.get("ref")
            if kref:
                self._last_elements[str(kref)] = el
        return {
            "ok": True,
            "url": res.get("url", ""),
            "title": res.get("title", ""),
            "dom_hash": res.get("dom_hash", ""),
            "elements": [e.model_dump() for e in elements],
        }

    async def find_links(self) -> list[dict[str, str]]:
        res = await self._request("GET", f"/sessions/{self._sid()}/links")
        return list(res.get("links", [])) if res.get("ok") else []

    async def find_forms(self) -> list[dict[str, Any]]:
        res = await self._request("GET", f"/sessions/{self._sid()}/forms")
        return list(res.get("forms", [])) if res.get("ok") else []

    async def detect_technologies(self) -> list[str]:
        res = await self._request("GET", f"/sessions/{self._sid()}/technologies")
        return list(res.get("technologies", [])) if res.get("ok") else []

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _action(self, action: str, **args: Any) -> dict[str, Any]:
        body = {"action": action, **args}
        res = await self._request("POST", f"/sessions/{self._sid()}/actions", json_body=body)
        if res.get("ok"):
            return {"ok": True}
        err = res.get("error", {})
        return self._fail(err.get("kind", "exception"), err.get("detail", ""))

    async def click(self, ref: str) -> dict[str, Any]:
        return await self._action("click", ref=ref)

    async def fill(self, ref: str, value: str) -> dict[str, Any]:
        return await self._action("fill", ref=ref, value=value)

    async def select_option(self, ref: str, value: str) -> dict[str, Any]:
        return await self._action("select_option", ref=ref, value=value)

    async def set_checked(self, ref: str, checked: bool) -> dict[str, Any]:
        return await self._action("set_checked", ref=ref, checked=checked)

    async def hover(self, ref: str) -> dict[str, Any]:
        return await self._action("hover", ref=ref)

    async def scroll(self, dy: int = 400) -> bool:
        res = await self._request("POST", f"/sessions/{self._sid()}/scroll", json_body={"dy": dy})
        return bool(res.get("ok"))

    async def press(self, key: str) -> bool:
        res = await self._request("POST", f"/sessions/{self._sid()}/press", json_body={"key": key})
        return bool(res.get("ok"))

    async def mouse_click(self, x: float, y: float) -> bool:
        res = await self._request(
            "POST", f"/sessions/{self._sid()}/mouse_click", json_body={"x": x, "y": y}
        )
        return bool(res.get("ok"))

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    async def get_current_url(self) -> str:
        res = await self._request("GET", f"/sessions/{self._sid()}/snapshot")
        return str(res.get("url", "")) if res.get("ok") else ""

    async def get_title(self) -> str:
        res = await self._request("GET", f"/sessions/{self._sid()}/snapshot")
        return str(res.get("title", "")) if res.get("ok") else ""

    async def list_storage_keys(self, kind: str) -> list[str]:
        res = await self._request(
            "GET", f"/sessions/{self._sid()}/storage", json_body={"kind": kind}
        )
        return list(res.get("keys", [])) if res.get("ok") else []


__all__ = ["KernelRuntime"]
