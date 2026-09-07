"""CoverageEngine — measurable exploration completeness (rebuild v4).

The coverage engine OWNS exploration integrity. It answers exactly:

- What has been DISCOVERED (routes, elements, forms, endpoints,
  workflows, auth states)?
- What has been VISITED/INTERACTED?
- What is BLOCKED (loop escape / failures)?
- What unexplored target should the planner go to next?
- When is exploration complete?

The planner consults the coverage engine before every step. This replaces
the old fixed-route behavior (hard cap of 5 routes, phase flags) and the
"success because /login was visited" fallacy: completion is a COVERAGE
statement, not a route-visit statement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from ..logging import get_logger
from ..models import BrowserState

log = get_logger("sera.task.coverage")


def route_key_of(url: str) -> str:
    """The coverage route key for a URL — handles path routing
    (``http://x/products`` → ``/products``) AND hash routing
    (``http://x/#/login`` → ``/login``)."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.fragment.startswith("/"):
        frag = parsed.fragment
        # "/login?x=1" → "/login"
        return frag.split("?", 1)[0] or "/"
    path = parsed.path
    return path if path else ""


def normalize_route(route: str) -> str:
    """Collapse per-item routes into route PATTERNS.

    ``/product/1`` … ``/product/46`` all become ``/product/:id`` so the
    coverage frontier is the APPLICATION's route space, not its data
    cardinality. Patterns:
    - all-digit segment → ``:id``
    - UUID-ish segment → ``:uuid``
    - long hex (≥8) → ``:hash``
    """
    if not route:
        return route
    segments = route.strip("/").split("/")
    out: list[str] = []
    for seg in segments:
        if not seg:
            continue
        if seg.isdigit():
            out.append(":id")
        elif _UUID_RE.match(seg):
            out.append(":uuid")
        elif len(seg) >= 8 and _HEX_RE.match(seg):
            out.append(":hash")
        else:
            out.append(seg)
    return "/" + "/".join(out) if out else "/"


import re as _re

_UUID_RE = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = _re.compile(r"^[0-9a-fA-F]+$")


# Element roles worth interacting with, in priority order (links first —
# they lead to new routes; then forms/buttons).
_ROLE_PRIORITY = {
    "link": 0,
    "button": 1,
    "textbox": 2,
    "checkbox": 3,
    "radio": 3,
    "combobox": 3,
    "searchbox": 2,
    "spinbutton": 4,
    "slider": 5,
    "option": 5,
}


@dataclass
class ExplorationTarget:
    """One actionable, not-yet-explored target chosen by the engine."""

    kind: str  # "route" | "element" | "form"
    value: str  # route path, or element ref, or form ref
    url: str | None = None  # for routes
    reason: str = ""

    def as_step_args(self) -> dict[str, Any]:
        if self.kind == "route":
            return {"url": self.url, "force": False}
        if self.kind == "element":
            return {"ref": self.value}
        return {"ref": self.value}


@dataclass
class CoverageEngine:
    """Tracks discovered vs explored across six coverage dimensions."""

    # Discovered (grows as observation finds things).
    discovered_routes: set[str] = field(default_factory=set)
    discovered_elements: set[str] = field(default_factory=set)  # by ref
    discovered_forms: set[str] = field(default_factory=set)  # by action|method
    discovered_endpoints: set[str] = field(default_factory=set)  # method|url
    discovered_workflows: list[dict[str, Any]] = field(default_factory=list)
    discovered_auth_states: set[str] = field(default_factory=lambda: {"ANONYMOUS"})

    # Explored.
    visited_routes: set[str] = field(default_factory=set)
    interacted_elements: set[str] = field(default_factory=set)
    inspected_forms: set[str] = field(default_factory=set)
    auth_states_reached: set[str] = field(default_factory=set)
    # Route key → navigable absolute URL (hash-routing aware).
    route_urls: dict[str, str] = field(default_factory=dict)

    # Blocked (from the loop manager / failure classification).
    blocked_refs: set[str] = field(default_factory=set)
    blocked_routes: set[str] = field(default_factory=set)
    # v4: domains reached via redirects/external links — a security
    # observation (open-redirect evidence), never coverage targets.
    external_domains_observed: set[str] = field(default_factory=set)

    # Element roles we never bother interacting with directly.
    skip_roles: set[str] = field(default_factory=lambda: {"option", "slider"})
    # Target origin (scheme://host), locked on first ingestion.
    _target_origin: str | None = None

    # ------------------------------------------------------------------
    # Ingestion (called by the agent loop after each observation/action)
    # ------------------------------------------------------------------

    def _origin(self, url: str) -> str:
        try:
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}"
        except ValueError:
            return ""

    def _in_scope(self, url: str | None) -> bool:
        """Route coverage tracks the TARGET ORIGIN only.

        Redirects and external links can carry the agent off-origin (e.g.
        an open redirect to github.com). Those observations are EVIDENCE
        (app graph + network recorder), never coverage targets — otherwise
        the agent burns its budget exploring the wrong site.
        """
        if not url:
            return False
        if self._target_origin is None:
            return True  # scope not yet established
        return self._origin(url) == self._target_origin

    def ingest_state(self, state: BrowserState) -> None:
        """Absorb the current state: discovered routes/elements/forms.

        Off-origin observations (external redirects) are skipped for
        coverage — the app graph and network recorder still capture them
        as evidence.
        """
        # Lock the scope to the task target on first ingestion.
        if self._target_origin is None:
            origin = self._origin(state.target)
            if origin:
                self._target_origin = origin
        if not self._in_scope(state.current_url):
            return
        base = state.current_url or state.target
        for r in state.routes:
            self.discovered_routes.add(normalize_route(r))
        # Route → navigable URL map (hash-routing aware): prefer a live
        # link whose route key matches, else fall back to path-join.
        for l in state.links:
            if not l.internal:
                continue
            if l.href.startswith("http") and self._origin(l.href) != self._target_origin:
                continue
            key = normalize_route(route_key_of(l.href))
            if key and key != "/" and l.href.startswith(("http", "/", "#")):
                url = l.href if l.href.startswith("http") else urljoin(base, l.href)
                self.route_urls.setdefault(key, url)
        for el in state.interactive_elements:
            if el.ref:
                self.discovered_elements.add(el.ref)
        for f in state.forms:
            self.discovered_forms.add(f"{f.action}|{f.method}")

    def ingest_network(self, endpoints: list[str]) -> None:
        for ep in endpoints:
            self.discovered_endpoints.add(ep)

    def record_visit(self, url: str) -> None:
        if not self._in_scope(url):
            # External navigation: remember the domain as a security
            # observation, never as route coverage.
            self.external_domains_observed.add(self._origin(url))
            return
        key = normalize_route(route_key_of(url))
        if key and key != "/":
            self.visited_routes.add(key)

    def record_interaction(self, ref: str | None) -> None:
        if ref:
            self.interacted_elements.add(ref)

    def record_form_inspection(self, form_key: str) -> None:
        self.inspected_forms.add(form_key)

    def record_auth_state(self, auth_state: str) -> None:
        self.discovered_auth_states.add(auth_state)
        self.auth_states_reached.add(auth_state)

    def record_workflow(self, workflow: dict[str, Any]) -> None:
        key = f"{workflow.get('action')}|{workflow.get('route', '')}"
        if not any(
            f"{w.get('action')}|{w.get('route', '')}" == key for w in self.discovered_workflows
        ):
            self.discovered_workflows.append(workflow)

    def sync_blocked(self, loop) -> None:  # type: ignore[no-untyped-def]
        self.blocked_refs = set(loop.blocked_refs)
        self.blocked_routes = set(loop.blocked_routes)

    # ------------------------------------------------------------------
    # Target selection (the planner's deterministic compass)
    # ------------------------------------------------------------------

    def _route_blocked(self, route: str) -> bool:
        """A route is blocked if itself (or its absolute URL form) was
        abandoned by the loop manager."""
        if route in self.blocked_routes:
            return True
        norm = route.rstrip("/")
        for b in self.blocked_routes:
            if b.rstrip("/").endswith(norm) or norm.endswith(b.rstrip("/")):
                return True
        return False

    def unexplored_routes(self, state: BrowserState) -> list[str]:
        return [
            r
            for r in sorted(self.discovered_routes)
            if r not in self.visited_routes and not self._route_blocked(r)
        ]

    def unexplored_elements(self, state: BrowserState) -> list[dict[str, Any]]:
        """Not-yet-interacted, unblocked, actionable elements (with priority)."""
        out = []
        for el in state.interactive_elements:
            if not el.ref or el.disabled or not el.visible:
                continue
            if el.role in self.skip_roles:
                continue
            if el.ref in self.interacted_elements or el.ref in self.blocked_refs:
                continue
            out.append({"ref": el.ref, "role": el.role, "name": (el.name or "")[:60]})
        # Priority: links first (they lead to new routes), then inputs
        # (forms), then buttons.
        out.sort(key=lambda e: _ROLE_PRIORITY.get(e["role"], 9))
        return out

    def next_target(self, state: BrowserState) -> ExplorationTarget | None:
        """The next unexplored target, in deterministic priority order.

        Priority:
        1. Unvisited discovered routes (breadth-first application mapping).
        2. Uninteracted elements (priority-sorted).
        3. Uninspected forms.
        """
        loop = getattr(self, "_loop_ref", None)
        if loop is not None:
            self.sync_blocked(loop)
        # Routes first.
        routes = self.unexplored_routes(state)
        if routes:
            route = routes[0]
            # Prefer the OBSERVED navigable URL (hash-routing aware);
            # fall back to path-join for path-routed apps.
            url = self.route_urls.get(route) or urljoin(
                state.current_url or state.target, route
            )
            return ExplorationTarget(
                kind="route", value=route, url=url, reason=f"unexplored route {route}"
            )
        # Then elements.
        elements = self.unexplored_elements(state)
        if elements:
            el = elements[0]
            return ExplorationTarget(
                kind="element", value=el["ref"], reason=f"unexplored {el['role']} '{el['name']}'"
            )
        # Then forms.
        for f in state.forms:
            key = f"{f.action}|{f.method}"
            if key not in self.inspected_forms:
                # A form is "inspected" via its first field's ref, if live.
                live_field = next((fl for fl in f.fields if fl.ref), None)
                if live_field is not None:
                    return ExplorationTarget(
                        kind="form",
                        value=live_field.ref,
                        reason=f"uninspected form {f.action}",
                    )
                self.record_form_inspection(key)
        return None

    def attach_loop(self, loop) -> None:  # type: ignore[no-untyped-def]
        """Keep a live reference to the loop manager for blocked-sync."""
        self._loop_ref = loop

    # ------------------------------------------------------------------
    # Completion + reporting
    # ------------------------------------------------------------------

    def completion_reason(self, state: BrowserState) -> str | None:
        """Why exploration is complete (None = not complete)."""
        if self.unexplored_routes(state):
            return None
        if self.unexplored_elements(state):
            return None
        if not self._all_forms_inspected(state):
            return None
        total_blocked = len(self.blocked_refs) + len(self.blocked_routes)
        return (
            f"coverage complete: {len(self.visited_routes)}/{len(self.discovered_routes)} routes "
            f"visited, {len(self.interacted_elements)}/{len(self.discovered_elements)} elements "
            f"interacted, {len(self.discovered_endpoints)} endpoints observed, "
            f"{len(self.auth_states_reached)} auth states reached, "
            f"{total_blocked} target(s) blocked"
        )

    def _all_forms_inspected(self, state: BrowserState) -> bool:
        for f in state.forms:
            key = f"{f.action}|{f.method}"
            if key not in self.inspected_forms:
                return False
        return True

    def summary(self) -> dict[str, Any]:
        """Machine-readable coverage summary (report + SERA intake)."""
        route_pct = (
            100.0 * len(self.visited_routes) / len(self.discovered_routes)
            if self.discovered_routes
            else 0.0
        )
        elem_pct = (
            100.0 * len(self.interacted_elements) / len(self.discovered_elements)
            if self.discovered_elements
            else 0.0
        )
        form_pct = (
            100.0 * len(self.inspected_forms) / len(self.discovered_forms)
            if self.discovered_forms
            else 0.0
        )
        return {
            "routes": {
                "discovered": len(self.discovered_routes),
                "visited": len(self.visited_routes),
                "blocked": len(self.blocked_routes),
                "percent": round(route_pct, 1),
            },
            "elements": {
                "discovered": len(self.discovered_elements),
                "interacted": len(self.interacted_elements),
                "blocked": len(self.blocked_refs),
                "percent": round(elem_pct, 1),
            },
            "forms": {
                "discovered": len(self.discovered_forms),
                "inspected": len(self.inspected_forms),
                "percent": round(form_pct, 1),
            },
            "endpoints": {
                "discovered": len(self.discovered_endpoints),
            },
            "workflows": {
                "discovered": len(self.discovered_workflows),
            },
            "auth_states": {
                "discovered": sorted(self.discovered_auth_states),
                "reached": sorted(self.auth_states_reached),
            },
            "visited_route_list": sorted(self.visited_routes),
            "blocked_refs": sorted(self.blocked_refs),
            "blocked_routes": sorted(self.blocked_routes),
            "external_domains_observed": sorted(self.external_domains_observed),
        }


class _NullLoop:
    """Fallback when no loop manager is attached (unit tests)."""

    blocked_refs: set = set()
    blocked_routes: set = set()


__all__ = ["CoverageEngine", "ExplorationTarget"]
