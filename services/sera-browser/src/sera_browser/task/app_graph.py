"""ApplicationGraph — UI actions correlated with routes and API endpoints.

Rebuild v4, Phase 7-8. The graph answers: "which UI operation triggered
which network traffic and which route transition?" Example edge:

    click(e012 "Search") on /search
      → GET /rest/products/search?q=apple      (endpoint node)
      → stayed on /search                       (route transition)

This is real workflow evidence (not heuristic labels like the old
``workflows = ["spa-routing", "authentication-form"]``): every edge is an
observed (action, request-set, route-transition) triple recorded at
execution time by the agent loop, with the network delta attributed by
the ActionExecutor's recorder watermark.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..logging import get_logger

log = get_logger("sera.task.app_graph")

_MAX_EDGES = 400
_MAX_NODES = 800


def _endpoint_key(req: dict[str, Any]) -> str:
    """Stable endpoint identity: method + path (query excluded by default
    so /rest/search?q=a and ?q=b are the same endpoint)."""
    url = req.get("url") or ""
    method = (req.get("method") or "GET").upper()
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
        return f"{method} {path}"
    except ValueError:
        return f"{method} {url[:120]}"


@dataclass
class GraphEdge:
    step: int
    action: str
    ref: str | None
    url_before: str | None
    url_after: str | None
    route_changed: bool
    endpoints: list[str] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "ref": self.ref,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "route_changed": self.route_changed,
            "endpoints": self.endpoints,
            "ok": self.ok,
        }


@dataclass
class ApplicationGraph:
    """Observed application behavior graph for one task."""

    edges: list[GraphEdge] = field(default_factory=list)
    # Node sets for the summary.
    routes_seen: set[str] = field(default_factory=set)
    endpoints_seen: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Recording (called by the agent loop after each action)
    # ------------------------------------------------------------------

    def record_action(
        self,
        step: int,
        action: str,
        ref: str | None,
        url_before: str | None,
        url_after: str | None,
        attributed_requests: list[dict[str, Any]],
        *,
        ok: bool = True,
    ) -> GraphEdge | None:
        """Record one (action → requests → route-transition) edge."""
        if action in ("noop", "screenshot", "update_memory", "history"):
            return None
        endpoints = []
        for req in attributed_requests:
            if req.get("resource_type") in ("xhr", "fetch", "document") or req.get("graphql"):
                key = _endpoint_key(req)
                endpoints.append(key)
                self.endpoints_seen.add(key)
        route_before = urlparse(url_before or "").path or None
        route_after = urlparse(url_after or "").path or None
        route_changed = (route_before != route_after) and bool(route_after)
        if route_after:
            self.routes_seen.add(route_after)

        edge = GraphEdge(
            step=step,
            action=action,
            ref=ref,
            url_before=url_before,
            url_after=url_after,
            route_changed=route_changed,
            endpoints=endpoints,
            ok=ok,
        )
        if len(self.edges) < _MAX_EDGES:
            self.edges.append(edge)
        return edge

    # ------------------------------------------------------------------
    # Correlation + reporting
    # ------------------------------------------------------------------

    def workflows(self) -> list[dict[str, Any]]:
        """Observed workflows: action sequences that produced observable
        effects (route change or API traffic). Real evidence, not labels."""
        wfs: list[dict[str, Any]] = []
        for edge in self.edges:
            if not edge.ok:
                continue
            if edge.route_changed or edge.endpoints:
                wfs.append(
                    {
                        "step": edge.step,
                        "action": edge.action,
                        "ref": edge.ref,
                        "workflow": self._describe(edge),
                        "route": urlparse(edge.url_after or "").path or None,
                        "endpoints": edge.endpoints[:8],
                    }
                )
        return wfs[:80]

    @staticmethod
    def _describe(edge: GraphEdge) -> str:
        route_b = urlparse(edge.url_before or "").path or ""
        route_a = urlparse(edge.url_after or "").path or ""
        if edge.route_changed:
            return f"{edge.action} {route_b or 'start'} → {route_a}"
        if edge.endpoints:
            return f"{edge.action} on {route_a or 'page'} → {', '.join(edge.endpoints[:3])}"
        return f"{edge.action} on {route_a or 'page'}"

    def action_endpoint_map(self) -> list[dict[str, Any]]:
        """Compact action → endpoint correlation (SERA security intake)."""
        out: list[dict[str, Any]] = []
        for edge in self.edges:
            if edge.endpoints:
                out.append(
                    {
                        "action": edge.action,
                        "ref": edge.ref,
                        "endpoints": edge.endpoints,
                    }
                )
        return out[:120]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {
                "routes": sorted(self.routes_seen)[:_MAX_NODES],
                "endpoints": sorted(self.endpoints_seen)[:_MAX_NODES],
            },
            "edges": [e.as_dict() for e in self.edges],
            "workflows": self.workflows(),
            "action_endpoint_map": self.action_endpoint_map(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "routes": len(self.routes_seen),
            "endpoints": len(self.endpoints_seen),
            "edges": len(self.edges),
            "workflows": len(self.workflows()),
        }


__all__ = ["ApplicationGraph", "GraphEdge"]
