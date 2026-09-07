"""LLM-backed TaskPlanner.

Flow:
    task
    → observe state
    → LLM chooses next action (receives STRUCTURED BrowserState, not raw
      Playwright internals)
    → validate action against current state (ref must exist, navigate must
      pass SSRF, action name must be in the allowed set)
    → Playwright executes via ActionExecutor (ref-based, never raw Playwright)
    → observe again
    → repeat
    → finish

The LLM is NEVER allowed to execute arbitrary Playwright code. It returns a
strict JSON object; we validate it before handing it to ActionExecutor.

Vision is an OPTIONAL escalation path:
- The LLM client MAY support vision (`supports_vision`).
- The planner does NOT send screenshots on every step.
- Vision is only used when:
    - loop recovery triggers and the client supports vision, OR
    - a challenge page requires visual understanding.

Fallback to the deterministic planner happens when:
- the LLM client is unavailable
- the LLM call times out
- the LLM returns invalid/unparseable JSON
- the LLM returns an action that fails validation
- the LLM is cancelled mid-call

Loop detection, cancellation, timeouts, and same-URL idempotency are
preserved — the LLM planner delegates to LoopRecoveryManager just like the
deterministic planner does, and uses the same CancellationManager.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..cancel import CancellationManager, TaskCancelledError
from ..config import Settings
from ..logging import get_logger, log_event
from ..models import BrowserState
from .action_schema import (
    ActionValidationError,
    parse_llm_response,
    validate_action,
)
from .llm_client import (
    LLMClient,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .loop import LoopRecoveryManager
from .planner import Step, StepFeedback, TaskPlanner

log = get_logger("sera.task.llm_planner")


# Hard cap on the JSON state view sent to the LLM (chars). Keeps prompts cheap
# and prevents pathological pages from blowing the context window.
_MAX_STATE_VIEW_CHARS = 12_000
_MAX_ELEMENTS_IN_VIEW = 40
_MAX_RECENT_NETWORK = 12


SYSTEM_PROMPT = """You are SERA Browser Agent's planner.

You receive a STRUCTURED view of the current BrowserState (JSON) and a
high-level objective. You must choose exactly ONE next action.

You CANNOT execute arbitrary code. You can only choose an action from this
fixed set and provide its arguments:

NAVIGATION:
- navigate   : go to a URL. args: {url: string, force?: boolean}
- back       : browser back. args: {}
- forward    : browser forward. args: {}
- reload     : reload the page. args: {}

ELEMENT INTERACTION:
- click      : click an element by ref. args: {ref: string}
- fill       : type text into an input by ref. args: {ref: string, value: string}
- fill_form  : BULK fill multiple fields (preferred for forms). args: {fields: [{ref, value}, ...]}
- select     : choose an option in a <select> by ref. args: {ref: string, value: string}
- check      : check a checkbox/radio by ref. args: {ref: string}
- uncheck    : uncheck a checkbox/radio by ref. args: {ref: string}
- hover      : hover an element by ref. args: {ref: string}
- scroll     : scroll the page. args: {selector?: string, dy?: number, into_ref?: string}
- press      : press a keyboard key. args: {key: string} (e.g. "Enter", "Tab")
- mouse_click: click at coordinates (vision fallback). args: {x: number, y: number}

TABS:
- switch_tab : switch to a tab by id. args: {tab_id: string}
- close_tab  : close a tab by id. args: {tab_id: string}
- new_tab    : open a new tab. args: {url: string}

CAPTURE / EXTRACTION:
- screenshot : capture a screenshot. args: {label?: string}
- extract    : extract structured data. args: {prompt: string, schema?: object}
- search_page: regex search page content. args: {pattern: string}
- find_elements: count elements by selector. args: {selector: string}

FILE OPS:
- upload_file: upload a file to a file input. args: {ref: string, path: string}
- save_pdf   : save page as PDF. args: {path?: string}

ADVANCED:
- drag       : drag from one element to another. args: {start_ref: string, end_ref: string}
- evaluate   : run JavaScript (gated). args: {script: string}

MEMORY:
- update_memory : update the long-term memory field. args: {memory: string}

LIFECYCLE:
- finish     : the objective is satisfied; stop. args: {reason?: string, memory?: string}
- blocked    : the task cannot progress; stop. args: {reason?: string}

REFS: every interactive element in the state view has a `ref` field
(like "e017"). You MUST use one of those refs for click/fill/select/etc.
Refs come from the CURRENT snapshot only. Inventing a ref, guessing a
selector, or using a positional reference will be rejected and the
deterministic coverage planner will take over. Do NOT use refs listed in
`blocked_refs` — they are abandoned targets.

COVERAGE: the state view lists `unexplored_targets` (routes and elements
the coverage engine has not explored yet) and `coverage` metrics. Prefer
actions that advance coverage of unexplored targets. The task completes
when coverage is complete — NOT merely because a common route like
/login was visited.

BULK ACTIONS: prefer `fill_form` over multiple `fill` calls — it's 74% fewer
tool calls and measurably faster on forms.

MEMORY: use `update_memory` to record cross-step facts (e.g. "logged in as
admin", "order #12345 created"). The memory field persists across steps and
tabs, so it's the only way to remember facts when switching context.

NAVIGATE: only navigate to URLs that pass the target's security policy. The
state view lists `allowed_routes` you can use. Navigating to the current URL
is a no-op (the executor is idempotent) — only do it if you have a reason.

FAILURE RECOVERY: if an action fails, do NOT retry the same action. Try a
fundamentally different approach (different ref, different action, scroll to
find the element, or declare blocked).

You MUST respond with a single JSON object and NOTHING else, in this exact
shape:

{
  "thinking": "<one short sentence about why>",
  "action": "<one of the actions above>",
  "args": { ... },
  "reason": "<one short sentence>"
}

Do not include markdown fences. Do not include commentary outside the JSON.
"""


@dataclass
class LLMPlannerStats:
    llm_calls: int = 0
    llm_succeeded: int = 0
    llm_failed: int = 0
    llm_unavailable: int = 0
    llm_timeout: int = 0
    llm_cancelled: int = 0
    fallback_to_deterministic: int = 0
    invalid_action: int = 0
    vision_calls: int = 0
    vision_succeeded: int = 0
    # Times a tool-repetition loop was handled by re-prompting the LLM with
    # an anti-repetition directive (instead of declaring the task blocked).
    repetition_redirects: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "llm_succeeded": self.llm_succeeded,
            "llm_failed": self.llm_failed,
            "llm_unavailable": self.llm_unavailable,
            "llm_timeout": self.llm_timeout,
            "llm_cancelled": self.llm_cancelled,
            "fallback_to_deterministic": self.fallback_to_deterministic,
            "invalid_action": self.invalid_action,
            "vision_calls": self.vision_calls,
            "vision_succeeded": self.vision_succeeded,
            "repetition_redirects": self.repetition_redirects,
        }


class LLMTaskPlanner:
    """LLM-backed planner with deterministic fallback.

    Implements the same `next_step(state, loop) -> Step` interface as
    `TaskPlanner`, so it's a drop-in replacement in `BrowserAgent`.

    v4 invariants:
    - The LLM may ONLY pick refs from the current ElementRegistry view;
      invented refs are rejected by validation before execution.
    - Vision is budgeted: at most ``vision_max_calls`` calls per task and a
      circuit breaker disables it after ``vision_failure_limit``
      consecutive provider failures. Provider errors NEVER become a
      strategy — on vision failure we fall straight to the deterministic
      coverage planner.
    - The state view includes coverage hints (unexplored targets), blocked
      refs, and the last structured step feedback.
    """

    def __init__(
        self,
        settings: Settings,
        client: LLMClient,
        cancellation: CancellationManager,
        objective: str,
        *,
        deterministic: TaskPlanner | None = None,
        timeout_s: float | None = None,
        screenshot_provider: Any = None,
        memory: Any = None,
        safety_guard: Any = None,
        tab_manager: Any = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.cancellation = cancellation
        self.objective = objective
        self._deterministic = deterministic or TaskPlanner(settings)
        self.timeout_s = timeout_s or 30.0
        self._screenshot_provider = screenshot_provider
        self._memory = memory
        self._safety_guard = safety_guard
        self._tab_manager = tab_manager
        self.stats = LLMPlannerStats()
        # Anti-repetition redirect state (see next_step).
        self._anti_repetition: str | None = None
        self._semantic_loop_note: str | None = None
        self._forbidden: set[str] = set()
        self._repetition_redirects = 0
        self._max_repetition_redirects = 3
        # v4: coverage hints + registry view.
        self._coverage: Any = None
        self._registry: Any = None
        self._last_feedback: StepFeedback | None = None
        # v4: vision budget + circuit breaker.
        self._vision_calls = 0
        self._vision_consecutive_failures = 0
        self._vision_disabled = False

    # ------------------------------------------------------------------
    # v4 wiring
    # ------------------------------------------------------------------

    def attach_coverage(self, coverage: Any) -> None:
        """Share the CoverageEngine with the deterministic planner so the
        LLM sees unexplored-target hints and blocked targets."""
        self._coverage = coverage

    def attach_registry(self, registry: Any) -> None:
        """Attach the ElementRegistry for the compact element view."""
        self._registry = registry

    def receive_feedback(self, feedback: StepFeedback) -> None:
        """v4 feedback channel: the planner sees the structured result of
        the last executed step."""
        self._last_feedback = feedback

    def _vision_budget_exceeded(self) -> bool:
        max_calls = getattr(self.settings, "vision_max_calls", 2)
        if self._vision_calls >= max_calls:
            return True
        return self._vision_disabled

    def _vision_note_failure(self) -> None:
        limit = getattr(self.settings, "vision_failure_limit", 2)
        self._vision_consecutive_failures += 1
        if self._vision_consecutive_failures >= limit:
            self._vision_disabled = True
            log.warning(
                "vision circuit breaker OPEN (consecutive provider failures)",
                extra={"event": "planner.vision.circuit_open", "failures": self._vision_consecutive_failures},
            )

    async def next_step(self, state: BrowserState, loop: LoopRecoveryManager) -> Step:
        # Loop recovery ALWAYS takes priority, regardless of LLM availability.
        recovery = loop.suggest_recovery(state)
        if recovery == "stop_repeating_action" or recovery == "abandon_target":
            # v4: both loop signals mean the same thing for the LLM path —
            # the current target is dead. Re-prompt the LLM with an
            # anti-repetition/abandonment directive instead of hard-blocking
            # the task. The LLM is (usually) still healthy — it just got
            # stuck repeating one action. Give it a bounded number of
            # redirects; after that, fall back to the deterministic coverage
            # planner (which picks a DIFFERENT unexplored target).
            if (
                self.client.available()
                and self._repetition_redirects < self._max_repetition_redirects
            ):
                repeated = loop.tool_loop() or "the previous action"
                loop.reset_tool_history()
                self._anti_repetition = (
                    "LOOP RECOVERY: your previous action \""
                    + repeated
                    + "\" was repeated without making progress and is now "
                    + "FORBIDDEN. You MUST choose a DIFFERENT action that "
                    + "advances the objective (navigate somewhere new, click "
                    + "another element, fill a form, observe, or finish). "
                    + "Do NOT repeat the forbidden action."
                )
                self._forbidden.add(repeated)
                self._repetition_redirects += 1
                self.stats.repetition_redirects += 1
                log.warning(
                    "tool repetition detected; redirecting LLM with anti-repetition directive",
                    extra={"event": "planner.loop.redirect", "repeated": repeated},
                )
                # Fall through to the normal LLM call path below.
            else:
                self.stats.fallback_to_deterministic += 1
                return Step(
                    kind="blocked",
                    action="noop",
                    args={},
                    reason="tool repetition persists after redirects; deterministic recovery",
                )
        if recovery in ("try_alternate_observation", "abandon_target", "stop_repeating_action"):
            # v4 (second check after the redirect branch above): loop escape
            # is TARGET ABANDONMENT. Vision is consulted ONLY as an optional
            # prioritization hint — within its budget — and NEVER as the
            # escape mechanism itself (the old behavior burned steps on
            # screenshot↔observe ping-pong).
            if (
                self.client.supports_vision
                and self._screenshot_provider is not None
                and not self._vision_budget_exceeded()
                and recovery != "stop_repeating_action"
            ):
                step = await self._try_vision_escalation(state, loop)
                if step is not None:
                    return step
            if self.client.available():
                loop.reset_dom_history()
                loop.reset_tool_history()
                self._semantic_loop_note = (
                    "LOOP RECOVERY: your recent actions are not making "
                    "progress (page/URL unchanged). The current target has "
                    "been ABANDONED and is now blocked. You MUST choose a "
                    "DIFFERENT unexplored target — see `unexplored_targets` "
                    "and avoid `blocked_refs` in the state view."
                )
                log.info(
                    "semantic loop detected; redirecting LLM with target-abandonment directive",
                    extra={"event": "planner.semantic_loop.redirect"},
                )
                # Fall through to the normal LLM call path below.

        # If the LLM client isn't available, fall back immediately.
        if not self.client.available():
            self.stats.llm_unavailable += 1
            self.stats.fallback_to_deterministic += 1
            log.info(
                "LLM unavailable; using deterministic planner",
                extra={"event": "planner.llm.unavailable"},
            )
            return await self._deterministic.next_step(state, loop)

        # Build the structured state view.
        state_view = self._build_state_view(state)

        # Call the LLM with a bounded timeout + cancellation.
        self.stats.llm_calls += 1
        try:
            response = await self.client.complete(
                system=SYSTEM_PROMPT,
                user=self._build_user_prompt(state_view, state),
                timeout_s=self.timeout_s,
                cancellation=self.cancellation,
            )
            self.stats.llm_succeeded += 1
        except TaskCancelledError:
            self.stats.llm_cancelled += 1
            self.stats.fallback_to_deterministic += 1
            log.info(
                "LLM call cancelled; using deterministic planner",
                extra={"event": "planner.llm.cancelled"},
            )
            # Re-raise so the orchestrator's cancellation handler fires.
            raise
        except LLMTimeoutError:
            self.stats.llm_timeout += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM call timed out; using deterministic planner",
                extra={"event": "planner.llm.timeout", "timeout_s": self.timeout_s},
            )
            return await self._deterministic.next_step(state, loop)
        except LLMUnavailableError as e:
            self.stats.llm_unavailable += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM unavailable; using deterministic planner",
                extra={"event": "planner.llm.unavailable", "err": str(e)},
            )
            return await self._deterministic.next_step(state, loop)
        except Exception as e:  # noqa: BLE001
            self.stats.llm_failed += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM call failed; using deterministic planner",
                extra={"event": "planner.llm.failed", "err": str(e)},
            )
            return await self._deterministic.next_step(state, loop)

        # Parse the response.
        try:
            parsed = parse_llm_response(response.content)
        except ActionValidationError as e:
            self.stats.invalid_action += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM returned unparseable response; using deterministic planner",
                extra={
                    "event": "planner.llm.invalid_json",
                    "err": str(e),
                    "response_preview": response.content[:200],
                },
            )
            return await self._deterministic.next_step(state, loop)

        # Validate the action against the current state.
        result = validate_action(parsed, state, self.settings)
        if not result.ok or result.step is None:
            self.stats.invalid_action += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM action rejected by validator; using deterministic planner",
                extra={
                    "event": "planner.llm.invalid_action",
                    "reason": result.reason,
                    "action": parsed.get("action"),
                },
            )
            return await self._deterministic.next_step(state, loop)

        step = result.step
        # Hard guard: if the LLM ignored the anti-repetition directive and
        # returned an action we forbade, treat it as invalid and fall back to
        # the deterministic planner instead of executing the loop again.
        step_key = f"{step.action}({step.args.get('url') or step.args.get('ref') or step.args.get('selector') or step.args.get('key') or ''})"
        if step_key in self._forbidden:
            self.stats.invalid_action += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "LLM returned forbidden (repetition-loop) action; using deterministic planner",
                extra={"event": "planner.llm.forbidden_action", "action": step_key},
            )
            return await self._deterministic.next_step(state, loop)

        # A different action was accepted — the repetition episode is over.
        # Clear the forbidden set so the LLM can legitimately re-use an
        # action later in the task (e.g. a second click on a button that
        # only becomes meaningful after other steps).
        if self._forbidden:
            self._forbidden.clear()

        log_event(
            log,
            20,
            "LLM chose action",
            action=step.action,
            args=step.args,
            reason=step.reason,
        )
        return step

    # ------------------------------------------------------------------
    # Vision escalation (optional)
    # ------------------------------------------------------------------

    async def _try_vision_escalation(
        self, state: BrowserState, loop: LoopRecoveryManager
    ) -> Step | None:
        """Vision escalation path — used only when loop recovery triggers.

        Captures a screenshot and asks the vision-capable LLM to choose the
        next action. Returns a validated Step, or None on any failure (in
        which case the caller falls back to a plain screenshot capture).

        This is the ONLY code path that sends an image to the LLM. The
        planner never sends screenshots on every step.
        """
        self.cancellation.check()
        if self._screenshot_provider is None:
            return None
        if self._vision_budget_exceeded():
            return None
        try:
            image_png = await self.cancellation.wait_or_cancel(
                self._screenshot_provider(), timeout=15.0
            )
        except TaskCancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning(
                "vision escalation: screenshot capture failed",
                extra={"event": "planner.vision.screenshot.fail", "err": str(e)},
            )
            return None
        if not image_png:
            return None

        self.stats.vision_calls += 1
        self.stats.llm_calls += 1
        self._vision_calls += 1
        try:
            response = await self.client.complete_with_vision(
                system=SYSTEM_PROMPT,
                user=self._build_user_prompt(self._build_state_view(state), state)
                + "\n\nA screenshot of the current page is attached. Use it "
                "to understand the visual layout and choose the next action.",
                image_png=image_png,
                timeout_s=self.timeout_s,
                cancellation=self.cancellation,
            )
            self.stats.llm_succeeded += 1
            self.stats.vision_succeeded += 1
            # Provider success resets the consecutive-failure breaker.
            self._vision_consecutive_failures = 0
        except TaskCancelledError:
            self.stats.llm_cancelled += 1
            self.stats.fallback_to_deterministic += 1
            raise
        except LLMTimeoutError:
            self.stats.llm_timeout += 1
            self.stats.fallback_to_deterministic += 1
            self._vision_note_failure()
            log.warning(
                "vision LLM call timed out",
                extra={"event": "planner.vision.timeout"},
            )
            return None
        except LLMUnavailableError as e:
            self.stats.llm_unavailable += 1
            self.stats.fallback_to_deterministic += 1
            self._vision_note_failure()
            log.warning(
                "vision LLM unavailable",
                extra={"event": "planner.vision.unavailable", "err": str(e)},
            )
            return None
        except Exception as e:  # noqa: BLE001
            self.stats.llm_failed += 1
            self.stats.fallback_to_deterministic += 1
            self._vision_note_failure()
            log.warning(
                "vision LLM call failed",
                extra={"event": "planner.vision.failed", "err": str(e)},
            )
            # v4: provider errors never become a strategy — go straight
            # to the deterministic coverage planner, no screenshot loop.
            return None

        try:
            parsed = parse_llm_response(response.content)
        except ActionValidationError as e:
            self.stats.invalid_action += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "vision LLM returned unparseable response",
                extra={"event": "planner.vision.invalid_json", "err": str(e)},
            )
            return None

        result = validate_action(parsed, state, self.settings)
        if not result.ok or result.step is None:
            self.stats.invalid_action += 1
            self.stats.fallback_to_deterministic += 1
            log.warning(
                "vision LLM action rejected by validator",
                extra={
                    "event": "planner.vision.invalid_action",
                    "reason": result.reason,
                    "action": parsed.get("action"),
                },
            )
            return None

        log_event(
            log,
            20,
            "vision LLM chose action",
            action=result.step.action,
            args=result.step.args,
            reason=result.step.reason,
        )
        return result.step

    # ------------------------------------------------------------------
    # State view builder
    # ------------------------------------------------------------------

    def _build_state_view(self, state: BrowserState) -> dict[str, Any]:
        """Build a compact, JSON-safe view of BrowserState for the LLM.

        Uses CACHE-FRIENDLY ORDERING (survey §7.2): stable context first,
        most-volatile last. This maximizes prefix-cache hits (~89% cost
        reduction on long workflows).

        v4 additions: registry element view (authoritative refs),
        unexplored-target hints from the coverage engine, blocked refs,
        and the last structured step feedback.
        """
        # v4: the registry's authoritative element view (when attached);
        # fallback to state.interactive_elements (same objects).
        if self._registry is not None:
            elements = [
                {
                    "ref": v["ref"],
                    "role": v["role"],
                    "name": v["name"][:60],
                    "type": v.get("type"),
                    "href": (v.get("href") or "")[:80] or None,
                    "placeholder": v.get("placeholder"),
                    "disabled": v.get("disabled", False),
                }
                for v in self._registry.view(max_elements=_MAX_ELEMENTS_IN_VIEW)
            ]
        else:
            elements = [
                {
                    "ref": el.ref,
                    "role": el.role,
                    "name": el.name,
                    "tag": el.tag,
                    "type": el.type,
                    "href": el.href,
                    "placeholder": el.placeholder,
                    "disabled": el.disabled,
                }
                for el in state.interactive_elements[:_MAX_ELEMENTS_IN_VIEW]
                if el.ref
            ]

        # Compact forms.
        forms = []
        for f in state.forms[:6]:
            forms.append(
                {
                    "ref": f.ref,
                    "action": f.action,
                    "method": f.method,
                    "has_password": f.has_password,
                    "has_csrf": f.has_csrf,
                    "field_count": len(f.fields),
                }
            )

        # Recent network summary.
        recent_requests = []
        for r in list(state.requests)[-_MAX_RECENT_NETWORK:]:
            recent_requests.append(
                {
                    "method": r.method,
                    "url": r.url,
                    "status": r.status,
                    "resource_type": r.resource_type,
                    "graphql": r.graphql,
                }
            )

        # Memory view (if memory manager is wired).
        memory_view: dict[str, Any] = {}
        if self._memory is not None:
            memory_view = self._memory.to_view(max_recent=8)

        # Tabs view (if tab manager is wired).
        tabs_view: list[dict[str, Any]] = []
        if self._tab_manager is not None:
            tabs_view = self._tab_manager.to_view()

        # v4: coverage hints — unexplored targets + blocked refs.
        unexplored: list[dict[str, Any]] = []
        if self._coverage is not None:
            for r in self._coverage.unexplored_routes(state)[:10]:
                unexplored.append({"kind": "route", "value": r})
            for el in self._coverage.unexplored_elements(state)[:10]:
                unexplored.append({"kind": "element", "value": el["ref"], "role": el["role"]})
        blocked_refs = sorted(
            set(getattr(self._coverage, "blocked_refs", None) or set())
            | set(getattr(self._deterministic.coverage, "blocked_refs", None) or set())
        )

        # v4: last structured step feedback.
        feedback_view: dict[str, Any] | None = None
        if self._last_feedback is not None:
            fb = self._last_feedback
            feedback_view = {
                "step": fb.step,
                "action": fb.action,
                "ok": fb.ok,
                "failure": fb.failure_kind,
                "route_changed": fb.route_changed,
                "url_after": fb.url_after,
                "new_refs": fb.new_refs[:12],
                "new_routes": fb.new_routes[:8],
                "network_events": len(fb.attributed_requests),
                "data_keys": fb.data_keys,
            }

        # Cache-friendly ordered view.
        view = {
            # 1. Stable per-task context (prefix-cacheable)
            "target": state.target,
            "objective": self.objective,
            # 2. Slowly-changing state
            "technologies": list(state.technologies),
            "authenticated": state.authenticated,
            "auth_state": state.auth_state or "ANONYMOUS",
            "user_role": state.user_role,
            "routes": list(state.routes),
            "api_endpoints": list(getattr(state, "api_endpoints", []) or []),
            # 3. Memory (changes slowly)
            "memory": memory_view,
            # 4. Tabs (change occasionally)
            "tabs": tabs_view,
            # 5. Recent step history (bounded, changes every step)
            "recent_observations": list(state.observations)[-5:],
            "challenges": [
                {"type": c.type, "handled": c.handled, "handling": c.handling}
                for c in state.challenges
            ],
            "console_error_count": len(state.console_errors),
            "page_error_count": len(state.page_errors),
            "websocket_endpoints": list(
                {w.url for w in state.websockets if w.event == "open"}
            ),
            "step_count": state.timings.total_steps,
            "max_steps": self.settings.max_steps,
            "screenshot_count": len(state.screenshots),
            # v4: coverage + feedback channel
            "unexplored_targets": unexplored,
            "blocked_refs": blocked_refs,
            "last_step_feedback": feedback_view,
            # 6. Current snapshot (most volatile — last for cache efficiency)
            "current_url": state.current_url,
            "title": state.title,
            "page_type": state.page_type,
            "dom_hash": (state.dom_hash or "")[:12],
            "interactive_elements": elements,
            "forms": forms,
            "recent_requests": recent_requests,
        }
        return view

    def _build_user_prompt(self, view: dict[str, Any], state: BrowserState) -> str:
        """Construct the user-facing prompt sent to the LLM.

        Uses cache-friendly ordering: objective → stable context → memory →
        tabs → history → current snapshot. The most-volatile part (current
        snapshot) is LAST so prefix caching works.

        In flash_mode, the reflection instructions are shortened to reduce
        token cost for routine steps.
        """
        # Compact JSON, sorted keys for determinism within each section.
        blob = json.dumps(view, ensure_ascii=False, sort_keys=True, default=str)
        # Hard cap to keep prompt size bounded.
        if len(blob) > _MAX_STATE_VIEW_CHARS:
            blob = blob[:_MAX_STATE_VIEW_CHARS] + "\n... (truncated)"
        directive = ""
        if self._anti_repetition:
            directive = "\n\n" + self._anti_repetition + "\n"
            # One-shot: the directive applies to this call only.
            self._anti_repetition = None
        if self._semantic_loop_note:
            directive += "\n" + self._semantic_loop_note + "\n"
            # One-shot: the note applies to this call only.
            self._semantic_loop_note = None
        if getattr(self.settings, "flash_mode", False):
            # Flash mode: minimal instructions, no reflection.
            return (
                "OBJECTIVE:\n"
                f"{self.objective}\n\n"
                "STATE:\n"
                f"{blob}\n{directive}\n"
                "Next action (JSON: {action, args}):"
            )
        return (
            "OBJECTIVE:\n"
            f"{self.objective}\n\n"
            "CURRENT BROWSER STATE (JSON):\n"
            f"{blob}\n{directive}\n"
            "Choose exactly ONE next action. Respond with a single JSON "
            "object in the shape {thinking, action, args, reason}. "
            "Do not include any other text."
        )


__all__ = ["LLMTaskPlanner", "LLMPlannerStats", "SYSTEM_PROMPT"]
