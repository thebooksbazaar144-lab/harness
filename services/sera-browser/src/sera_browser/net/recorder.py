"""NetworkRecorder — structured request/response/WebSocket capture.

- Captures method, URL, status, request/response headers (redacted),
  sizes, resource type, initiator, timing, auth indicators, GraphQL ops,
  WebSocket events, redirects.
- Sensitive headers are redacted unless `Settings.log_sensitive` is True
  AND the target is an owned lab.
- Reusable as a security sensor for SERA.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError, Request, Response, WebSocket

from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger, safe_repr_headers
from ..models import NetworkRequest, WebSocketEvent

log = get_logger("sera.net.recorder")


_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "proxy-authorization",
}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: ("***REDACTED***" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


@dataclass
class NetworkRecorder:
    """Records structured traffic. Independent of the agent loop.

    SERA can later consume this directly as a security sensor.

    v4: API-like endpoints are ALSO kept in a persistent, never-evicting
    ordered set. The rolling ``requests`` deque (maxlen=1000) evicts early
    requests during long explorations — on API-heavy SPAs the /rest/*
    startup calls fell out the back before the report was built, so
    ``api_endpoints()`` returned []. The persistent set fixes that.
    """

    settings: Settings
    cancellation: CancellationManager
    requests: deque[NetworkRequest] = field(default_factory=lambda: deque(maxlen=1000))
    responses: deque[NetworkRequest] = field(default_factory=lambda: deque(maxlen=1000))
    websockets: deque[WebSocketEvent] = field(default_factory=lambda: deque(maxlen=200))
    _pending: dict[str, NetworkRequest] = field(default_factory=dict)
    # v4: persistent endpoint identities (insertion-ordered, never evicted).
    _endpoint_keys: "dict[str, None]" = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def attach(self, page) -> None:  # type: ignore[no-untyped-def]
        """Wire Playwright page events. Safe to call once per page."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_request(self, req: Request) -> None:
        if self.cancellation.cancelled:
            return
        try:
            headers = _redact_headers(req.headers)
            has_auth = any(h.lower() in _SENSITIVE_HEADERS for h in req.headers.keys())
            has_cookie = bool(req.headers.get("cookie"))
            post_data = None
            try:
                post_data = req.post_data
            except PlaywrightError:
                pass
            graphql_op = self._detect_graphql(req.method, req.url, post_data)
            # v4: persist API-like endpoint identities (never evicted).
            try:
                self._note_endpoint(req.url)
            except Exception:  # noqa: BLE001
                pass
            rec = NetworkRequest(
                id=f"r-{id(req)}-{time.time_ns()}",
                method=req.method,
                url=req.url,
                resource_type=req.resource_type,
                request_headers=headers,
                request_size=len(post_data) if post_data else None,
                initiator=req.frame.url if req.frame else None,
                post_data=post_data if self.settings.log_sensitive else None,
                has_authorization=has_auth,
                has_cookie=has_cookie,
                graphql=bool(graphql_op),
                graphql_operation=graphql_op,
            )
            self._pending[id(req)] = rec
            self.requests.append(rec)
        except Exception as e:  # noqa: BLE001 — recorder must never break the page
            log.debug("request record failed", extra={"event": "net.req.fail", "err": str(e)})

    def _on_response(self, resp: Response) -> None:
        if self.cancellation.cancelled:
            return
        try:
            req = resp.request
            rec = self._pending.get(id(req))
            timing = None
            try:
                # request.timing is a dict with DNS/connect/etc.
                t = req.timing
                if t and isinstance(t, dict):
                    total = (
                        t.get("requestTime")
                        and t.get("responseEnd")
                        and (t["responseEnd"] - t["requestTime"]) * 1000.0
                    )
                    timing = total if isinstance(total, (int, float)) and total > 0 else None
            except (AttributeError, KeyError, TypeError):
                pass
            headers = _redact_headers(resp.headers)
            # from_cache was removed in newer Playwright; detect via headers.
            from_cache = (
                resp.headers.get("x-from-cache") == "true"
                or resp.headers.get("age") is not None
            )
            if rec is not None:
                rec.status = resp.status
                rec.response_headers = headers
                rec.response_size = None
                rec.mime_type = resp.headers.get("content-type")
                rec.from_cache = from_cache
                rec.timing_ms = timing
                rec.redirect_from = (
                    req.redirected_from.url if req.redirected_from else None
                )
                self.responses.append(rec)
            else:
                # Orphan response (rare). Synthesize a record.
                rec = NetworkRequest(
                    id=f"orphan-{time.time_ns()}",
                    method=req.method,
                    url=req.url,
                    resource_type=req.resource_type,
                    request_headers=_redact_headers(req.headers),
                    status=resp.status,
                    response_headers=headers,
                    mime_type=resp.headers.get("content-type"),
                    from_cache=from_cache,
                    timing_ms=timing,
                )
                self.responses.append(rec)
        except Exception as e:  # noqa: BLE001
            log.debug("response record failed", extra={"event": "net.resp.fail", "err": str(e)})

    def _on_websocket(self, ws: WebSocket) -> None:
        try:
            ws_id = f"ws-{id(ws)}-{time.time_ns()}"
            self.websockets.append(
                WebSocketEvent(id=ws_id, url=ws.url, event="open")
            )

            def _on_close(self_ws=ws, _id=ws_id) -> None:
                self.websockets.append(
                    WebSocketEvent(id=_id, url=self_ws.url, event="close")
                )

            def _on_frame_sent(payload, _id=ws_id) -> None:
                preview = (payload[:120] if isinstance(payload, str) else "<binary>") if self.settings.log_sensitive else "***REDACTED***"
                self.websockets.append(
                    WebSocketEvent(id=_id, url=ws.url, event="frame_sent", payload_preview=preview)
                )

            def _on_frame_received(payload, _id=ws_id) -> None:
                preview = (payload[:120] if isinstance(payload, str) else "<binary>") if self.settings.log_sensitive else "***REDACTED***"
                self.websockets.append(
                    WebSocketEvent(id=_id, url=ws.url, event="frame_received", payload_preview=preview)
                )

            def _on_error(msg, _id=ws_id) -> None:
                self.websockets.append(
                    WebSocketEvent(id=_id, url=ws.url, event="error", payload_preview=str(msg)[:200])
                )

            ws.on("close", _on_close)
            ws.on("framesent", _on_frame_sent)
            ws.on("framereceived", _on_frame_received)
            ws.on("socketerror", _on_error)
        except Exception as e:  # noqa: BLE001
            log.debug("websocket wire failed", extra={"event": "net.ws.wire.fail", "err": str(e)})

    # ------------------------------------------------------------------
    # Analysis helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_graphql(method: str, url: str, post_data: str | None) -> str | None:
        """Return GraphQL operation name if this looks like a GraphQL call."""
        try:
            path = urlparse(url).path.lower()
        except Exception:  # noqa: BLE001
            path = ""
        if "graphql" not in path and "/api/graphql" not in path:
            return None
        if method != "POST" or not post_data:
            return None
        # Naive op extraction. Avoids JSON parse cost in hot path.
        import re

        m = re.search(r'"operationName"\s*:\s*"([^"]+)"', post_data)
        if m:
            return m.group(1)
        return "unknown"

    # ------------------------------------------------------------------
    # Public view
    # ------------------------------------------------------------------

    def _note_endpoint(self, url: str) -> None:
        """Record an API-like endpoint identity persistently."""
        try:
            p = urlparse(url)
            host, path = p.netloc, p.path
        except Exception:  # noqa: BLE001
            return
        if not path:
            return
        if any(seg in path.lower() for seg in ("/api/", "/rest/", "/graphql", "/v1/", "/v2/")):
            self._endpoint_keys[f"{host}{path}"] = None

    def api_endpoints(self) -> list[str]:
        """Unique API-like URLs (deduplicated by path).

        v4: served from the persistent never-evicting set — immune to the
        rolling request deque's eviction during long explorations.
        """
        if self._endpoint_keys:
            return sorted(self._endpoint_keys.keys())
        # Fallback for recorders populated before v4 (SERA constructs
        # NetworkRecorder directly and may replay old deques).
        seen: set[str] = set()
        out: list[str] = []
        for r in list(self.requests):
            try:
                p = urlparse(r.url)
                host = p.netloc
                path = p.path
            except Exception:  # noqa: BLE001
                continue
            if not path:
                continue
            if any(seg in path.lower() for seg in ("/api/", "/rest/", "/graphql", "/v1/", "/v2/")):
                key = f"{host}{path}"
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        return sorted(out)

    def graphql_operations(self) -> list[str]:
        out = [r.graphql_operation for r in list(self.requests) if r.graphql]
        return sorted(set(out))

    def websocket_endpoints(self) -> list[str]:
        return sorted({w.url for w in list(self.websockets) if w.event == "open"})

    def auth_indicators(self) -> list[str]:
        out: set[str] = set()
        for r in list(self.requests):
            if r.has_authorization:
                out.add("Authorization header")
            if r.has_cookie:
                out.add("Cookie")
        # Detect Set-Cookie in responses
        for r in list(self.responses):
            if "set-cookie" in {k.lower() for k in r.response_headers}:
                out.add("Set-Cookie issued")
        return sorted(out)

    def redirects(self) -> list[tuple[str, str, int]]:
        out: list[tuple[str, str, int]] = []
        for r in list(self.responses):
            if r.redirect_from and r.status in {301, 302, 303, 307, 308}:
                out.append((r.redirect_from, r.url, r.status))
        return out

    def status_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in list(self.responses):
            if r.status is None:
                continue
            bucket = f"{r.status // 100}xx"
            out[bucket] = out.get(bucket, 0) + 1
        return out

    def summary(self) -> dict:
        return {
            "request_count": len(self.requests),
            "response_count": len(self.responses),
            "api_endpoints": self.api_endpoints(),
            "graphql_operations": self.graphql_operations(),
            "websocket_endpoints": self.websocket_endpoints(),
            "auth_indicators": self.auth_indicators(),
            "redirects": self.redirects(),
            "status_breakdown": self.status_breakdown(),
        }
