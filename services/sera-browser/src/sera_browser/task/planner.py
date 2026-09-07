"""TaskPlanner — deterministic, coverage-driven planning (rebuild v4).

Replaces the v3 fixed-route planner (hard cap of 5 routes, phase flags,
"objective satisfied" after visiting a couple of routes) with a planner
whose ONLY compass is the CoverageEngine:

    1. navigate to the target
    2. observe
    3. loop: coverage.next_target() → act on it (route / element / form)
       - loop escape → the current target is already blocked by the
         LoopRecoveryManager; the NEXT call to next_target() necessarily
         returns a DIFFERENT unexplored target (never a screenshot)
       - failed actions → failure classification; retry once; then the
         target is blocked and we move on
    4. completion: coverage.completion_reason() — a COVERAGE statement,
       never "we visited /login".

The LLM planner (llm_planner.py) sits ABOVE this planner for
prioritization, but its fallback and its unexplored-target hints come
from the same coverage engine — LLM unavailability degrades gracefully to
full deterministic exploration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..logging import get_logger
from ..models import BrowserState
from .coverage import CoverageEngine
from .loop import LoopRecoveryManager

log = get_logger("sera.task.planner")


StepKind = Literal[
    "navigate",
    "observe",
    "wait",
    "explore_routes",
    "inspect_forms",
    "attempt_safe_login",
    "capture_screenshot",
    "act",
    "finish",
    "blocked",
]


__all__ = ["Step", "StepKind", "TaskPlanner", "StepFeedback"]


@dataclass
class Step:
    kind: StepKind
    action: str
    args: dict
    reason: str


@dataclass
class StepFeedback:
    """Structured feedback from the last executed step (v4 feedback channel).

    The planner receives: the action's outcome, its classified failure,
    navigation changes, newly discovered elements/routes, the network
    traffic the action generated, and preserved extraction data. This
    restores the observe→reason feedback loop that v3 broke (extract was
    a fake success; scrape results were discarded; the LLM saw ≤200-char
    breadcrumbs).
    """

    step: int = 0
    action: str = ""
    ok: bool = True
    ref: str | None = None
    failure_kind: str | None = None
    detail: str = ""
    url_before: str | None = None
    url_after: str | None = None
    route_changed: bool = False
    new_refs: list[str] = field(default_factory=list)
    lost_refs: list[str] = field(default_factory=list)
    new_routes: list[str] = field(default_factory=list)
    attributed_requests: list[dict[str, Any]] = field(default_factory=list)
    data_keys: list[str] = field(default_factory=list)

    def compact(self) -> str:
        """Short human-readable line for observation breadcrumbs."""
        parts = [f"step {self.step}: {self.action}"]
        if self.ref:
            parts.append(f"ref={self.ref}")
        parts.append("ok" if self.ok else f"FAILED({self.failure_kind})")
        if self.route_changed:
            parts.append(f"navigated {self.url_before} → {self.url_after}")
        if self.new_routes:
            parts.append(f"new routes: {self.new_routes[:5]}")
        if self.attributed_requests:
            parts.append(f"{len(self.attributed_requests)} network events")
        if self.data_keys:
            parts.append(f"data: {self.data_keys}")
        return "; ".join(parts)


class TaskPlanner:
    """Deterministic coverage-driven planner.

    Decision inputs (in priority order):
    1. LoopRecoveryManager escapes — abandon current target.
    2. CoverageEngine targets — the exploration frontier.
    3. Auth credentials (optional) — ONE login attempt with postcondition
       verification, never a blind "mark authenticated".
    """

    def __init__(self, settings, coverage: CoverageEngine | None = None) -> None:  # type: ignore[no-untyped-def]
        self.settings = settings
        self.coverage = coverage if coverage is not None else CoverageEngine()
        self._navigated = False
        self._observed = False
        self._loop_escapes = 0
        self._auth_probed = False
        self._login_attempted = False
        self._login_submit_ref: str | None = None
        self._final_screenshot_taken = False
        self._last_feedback: StepFeedback | None = None
        self._tech_step_done = False

    def receive_feedback(self, feedback: StepFeedback) -> None:
        """Consume the structured result of the last executed step."""
        self._last_feedback = feedback
        if feedback.ok:
            if feedback.ref:
                self.coverage.record_interaction(feedback.ref)
        if feedback.url_after:
            self.coverage.record_visit(feedback.url_after)

    async def next_step(self, state: BrowserState, loop: LoopRecoveryManager) -> Step:  # noqa: C901
        # Keep the coverage engine's blocked sets in sync with the loop
        # manager (loop escapes block targets).
        self.coverage.sync_blocked(loop)
        self.coverage.ingest_state(state)

        # ---- Loop escape: abandon the stuck target. The loop manager
        # blocks it (blocked refs/routes) and resets its short-term
        # detectors; the coverage engine then necessarily picks a
        # DIFFERENT unexplored target below. NEVER a screenshot.
        signal = loop.detect(state)
        if signal is not None:
            self._loop_escapes += 1
            loop.escape(state)
            log.warning(
                "loop detected; abandoning current target",
                extra={"event": "planner.loop.escape", "signal": signal, "episode": self._loop_escapes},
            )
            # fall through to coverage-driven target selection below.

        # Phase 0: initial navigate.
        if state.current_url is None or state.current_url == "about:blank":
            self._navigated = False
        if not self._navigated and (state.current_url is None or state.current_url == "about:blank"):
            return Step(
                kind="navigate",
                action="navigate",
                args={"url": state.target},
                reason="initial navigation to target",
            )
        self._navigated = True

        # Phase 1: observe.
        if state.dom_hash is None:
            return Step(
                kind="observe",
                action="noop",
                args={},
                reason="observe current state",
            )

        # Phase 2: technology detection (genuinely ONCE).
        if not self._tech_step_done and not state.technologies:
            self._tech_step_done = True
            return Step(
                kind="observe",
                action="noop",
                args={},
                reason="detect technologies",
            )

        # Phase 3: ONE auth probe (navigate to /login only if a login link
        # was actually discovered — no blind guessing of common routes).
        if not self._auth_probed and self._should_probe_auth(state):
            self._auth_probed = True
            login_target = self._find_login_route(state)
            if login_target is not None:
                from urllib.parse import urljoin

                target = urljoin(state.current_url or state.target, login_target)
                return Step(
                    kind="explore_routes",
                    action="navigate",
                    args={"url": target},
                    reason=f"probe discovered auth route {login_target}",
                )

        # Phase 4: coverage-driven exploration — THE main loop.
        target = self.coverage.next_target(state)
        if target is not None:
            if target.kind == "route":
                return Step(
                    kind="explore_routes",
                    action="navigate",
                    args=target.as_step_args(),
                    reason=target.reason,
                )
            if target.kind == "element":
                # Prefer clicking links/buttons; textboxes need values we
                # don't invent — inspect forms instead (below).
                return Step(
                    kind="act",
                    action="click",
                    args=target.as_step_args(),
                    reason=target.reason,
                )
            if target.kind == "form":
                return Step(
                    kind="inspect_forms",
                    action="noop",
                    args={},
                    reason=target.reason,
                )

        # Phase 5: optional single login attempt when credentials were
        # provided AND a password form is live. Verification is evidence-
        # based (auth_state machine), never "attempted = authenticated".
        #
        # Two sub-steps: fill_form (credentials) then click(submit_ref).
        if self._login_submit_ref is not None and self._login_submit_ref not in loop.blocked_refs:
            submit_ref = self._login_submit_ref
            self._login_submit_ref = None
            return Step(
                kind="attempt_safe_login",
                action="click",
                args={"ref": submit_ref},
                reason="submit login (postconditions verified by auth state machine)",
            )
        if self._has_credentials() and not self._login_attempted:
            password_form = next(
                (f for f in state.forms if f.has_password and f.submit_ref), None
            )
            if password_form is not None:
                self._login_attempted = True
                return self._build_login_step(state, password_form)

        # Phase 6: final screenshot for the report (one, at the end).
        if not self._final_screenshot_taken and not state.screenshots:
            self._final_screenshot_taken = True
            return Step(
                kind="capture_screenshot",
                action="screenshot",
                args={"label": "final"},
                reason="capture final visual evidence",
            )

        # Phase 7: completion — a COVERAGE statement.
        reason = self.coverage.completion_reason(state)
        if reason is None:
            reason = "coverage frontier exhausted"
        return Step(
            kind="finish",
            action="noop",
            args={},
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _should_probe_auth(self, state: BrowserState) -> bool:
        # Probe only when the app actually advertises a login route.
        return bool(self._find_login_route(state))

    @staticmethod
    def _find_login_route(state: BrowserState) -> str | None:
        for r in state.routes:
            if any(h in r.lower() for h in ("login", "signin", "sign-in", "auth")):
                return r
        return None

    def _has_credentials(self) -> bool:
        auth = getattr(self.settings, "_task_auth", None)
        if not isinstance(auth, dict):
            return False
        return bool(auth.get("username") and auth.get("password"))

    def _build_login_step(self, state: BrowserState, form) -> Step:  # type: ignore[no-untyped-def]
        auth = getattr(self.settings, "_task_auth", {}) or {}
        username = auth.get("username", "")
        password = auth.get("password", "")
        # Find live field refs for user/password.
        user_ref = None
        pass_ref = None
        for fl in form.fields:
            if fl.type == "email" or "user" in (fl.name or "").lower() or "email" in (fl.name or "").lower():
                if fl.ref and user_ref is None:
                    user_ref = fl.ref
            elif fl.type == "password" and fl.ref:
                pass_ref = fl.ref
        fields = []
        if user_ref:
            fields.append({"ref": user_ref, "value": str(username)})
        if pass_ref:
            fields.append({"ref": pass_ref, "value": str(password)})
        if not fields or not form.submit_ref:
            return Step(
                kind="inspect_forms",
                action="noop",
                args={},
                reason="login form present but fields not actionable; inspecting only",
            )
        # Queue the submit click as the immediately-following step.
        self._login_submit_ref = form.submit_ref
        return Step(
            kind="attempt_safe_login",
            action="fill_form",
            args={"fields": fields},
            reason="fill credentials for single evidence-verified login attempt",
        )

    def note_login_submit(self, submit_ref: str | None) -> None:
        """Called by the agent after a login fill_form step so the next
        step submits the form."""
        self._login_submit_ref = submit_ref
