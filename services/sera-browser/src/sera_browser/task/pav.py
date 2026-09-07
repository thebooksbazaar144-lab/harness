"""Plan-Act-Validate multi-agent pattern (Skyvern-style).

Inspired by Skyvern's Planner/Actor/Validator/Navigator architecture and
the survey's finding that Plan-and-Execute + Validator/Reflexion beats raw
ReAct on long horizons and raw Plan-and-Execute on recoverability.

Architecture:
    Objective
        ↓
    Planner ── decomposes into ordered sub-goals
        ↓
    For each sub-goal:
        Actor ── executes via the existing LLMTaskPlanner + ActionExecutor
            ↓
        Validator ── checks if the sub-goal was achieved
            ↓ (fail)
        Reflexion ── records the failure, re-plans the remaining sub-goals
            ↓ (pass)
        next sub-goal
        ↓
    Final report

The Planner and Validator are LLM-backed (using the same provider-agnostic
LLMClient). The Actor reuses the existing LLMTaskPlanner so all advanced
features (memory, safety, observability) are inherited.

This pattern is OPT-IN via `SBA_AGENT_PATTERN=plan_act_validate`. The
default (`none`) uses the single-planner architecture.

When `agent_pattern="none"`, the BrowserAgent uses LLMTaskPlanner directly.
When `agent_pattern="plan_act_validate"`, the BrowserAgent wraps the
LLMTaskPlanner in a PlanActValidateOrchestrator.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from ..cancel import CancellationManager, TaskCancelledError
from ..config import Settings
from ..logging import get_logger, log_event
from ..models import BrowserState
from .action_schema import ActionValidationError, parse_llm_response
from .llm_client import LLMClient, LLMResponse, LLMTimeoutError, LLMUnavailableError
from .loop import LoopRecoveryManager
from .planner import Step, TaskPlanner

log = get_logger("sera.task.pav")


PLANNER_SYSTEM_PROMPT = """You are a task planner for a browser agent.

Given a high-level objective and the current browser state, decompose the
objective into a small ordered list of sub-goals. Each sub-goal should be:
- Concrete and verifiable (e.g. "navigate to /login", "fill the username field")
- Achievable in 1-5 browser actions
- Independent of future sub-goals (don't reference sub-goal #3 from #1)

Return a JSON array of sub-goal objects:
[
  {"id": 1, "goal": "navigate to the login page", "success_criteria": "URL contains /login"},
  {"id": 2, "goal": "fill the username field", "success_criteria": "username field is filled"}
]

Do not include markdown fences or commentary. Just the JSON array.
"""


VALIDATOR_SYSTEM_PROMPT = """You are a task validator for a browser agent.

Given a sub-goal, its success criteria, and the current browser state, decide
whether the sub-goal was achieved.

Return a single JSON object:
{
  "achieved": true | false,
  "reason": "<one short sentence explaining why>"
}

Do not include markdown fences or commentary. Just the JSON object.
"""


@dataclass
class SubGoal:
    """A single sub-goal in a plan."""
    id: int
    goal: str
    success_criteria: str = ""
    status: str = "pending"  # pending | in_progress | achieved | failed | skipped
    attempts: int = 0
    max_attempts: int = 2


@dataclass
class Plan:
    """A plan: an ordered list of sub-goals."""
    sub_goals: list[SubGoal] = field(default_factory=list)
    reasoning: str = ""

    def next_pending(self) -> SubGoal | None:
        for sg in self.sub_goals:
            if sg.status == "pending":
                return sg
        return None

    @property
    def all_achieved(self) -> bool:
        return all(sg.status == "achieved" for sg in self.sub_goals)

    @property
    def any_failed(self) -> bool:
        return any(sg.status == "failed" for sg in self.sub_goals)


@dataclass
class ValidationResult:
    """The outcome of validating a sub-goal."""
    achieved: bool
    reason: str = ""


@dataclass
class PAVStats:
    """Stats for the Plan-Act-Validate orchestrator."""
    plans_made: int = 0
    sub_goals_total: int = 0
    sub_goals_achieved: int = 0
    sub_goals_failed: int = 0
    validations_passed: int = 0
    validations_failed: int = 0
    reflexion_replans: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "plans_made": self.plans_made,
            "sub_goals_total": self.sub_goals_total,
            "sub_goals_achieved": self.sub_goals_achieved,
            "sub_goals_failed": self.sub_goals_failed,
            "validations_passed": self.validations_passed,
            "validations_failed": self.validations_failed,
            "reflexion_replans": self.reflexion_replans,
        }


class PlanActValidateOrchestrator:
    """Skyvern-style Plan-Act-Validate multi-agent orchestrator.

    Wraps an LLMTaskPlanner (the Actor) with a Planner and Validator that
    use the same LLMClient. Implements the same `next_step` interface so
    it's a drop-in replacement in BrowserAgent.

    Flow per `next_step` call:
    1. If no plan exists, call the Planner to decompose the objective.
    2. Get the next pending sub-goal.
    3. Call the Actor (LLMTaskPlanner) with the sub-goal as its objective.
    4. Call the Validator to check if the sub-goal was achieved.
    5. If achieved, mark done and return the Actor's step.
    6. If failed, increment attempts; if attempts < max, re-plan; else mark failed.

    The orchestrator runs ONE sub-goal per `next_step` call (the Actor's
    step for that sub-goal). The BrowserAgent's main loop calls `next_step`
    repeatedly until `finish` or `max_steps`.
    """

    def __init__(
        self,
        settings: Settings,
        client: LLMClient,
        cancellation: CancellationManager,
        objective: str,
        actor: Any,  # LLMTaskPlanner
        *,
        deterministic: TaskPlanner | None = None,
        timeout_s: float | None = None,
        max_sub_goals: int = 6,
    ) -> None:
        self.settings = settings
        self.client = client
        self.cancellation = cancellation
        self.objective = objective
        self.actor = actor
        self._deterministic = deterministic or TaskPlanner(settings)
        self.timeout_s = timeout_s or settings.llm_timeout_s
        self.max_sub_goals = max_sub_goals
        self.plan: Plan | None = None
        self.stats = PAVStats()

    async def next_step(self, state: BrowserState, loop: LoopRecoveryManager) -> Step:
        """Return the next step to execute.

        This is the Actor's step for the current sub-goal. If no plan
        exists, it first calls the Planner. If the current sub-goal was
        achieved (validated), it advances to the next one.
        """
        # Loop recovery always takes priority.
        recovery = loop.suggest_recovery(state)
        if recovery == "stop_repeating_action":
            return Step(
                kind="blocked",
                action="noop",
                args={},
                reason="tool repetition detected; PAV recovery",
            )

        # Make a plan if we don't have one.
        if self.plan is None:
            self.plan = await self._make_plan(state)
            if self.plan is None:
                # Planner failed → fall back to deterministic.
                return await self._deterministic.next_step(state, loop)

        # If all sub-goals are achieved, finish.
        if self.plan.all_achieved:
            return Step(
                kind="finish",
                action="noop",
                args={},
                reason="all sub-goals achieved",
            )

        # If any sub-goal failed beyond max_attempts, finish as blocked.
        if self.plan.any_failed:
            failed = [sg for sg in self.plan.sub_goals if sg.status == "failed"]
            return Step(
                kind="blocked",
                action="noop",
                args={},
                reason=f"sub-goal(s) failed: {[sg.id for sg in failed]}",
            )

        # Get the next pending sub-goal.
        sg = self.plan.next_pending()
        if sg is None:
            return Step(
                kind="finish",
                action="noop",
                args={},
                reason="no more pending sub-goals",
            )

        # Execute the sub-goal via the Actor (LLMTaskPlanner).
        sg.status = "in_progress"
        sg.attempts += 1
        # Temporarily override the Actor's objective with the sub-goal.
        original_objective = self.actor.objective
        self.actor.objective = f"Sub-goal {sg.id}: {sg.goal} (criteria: {sg.success_criteria})"
        try:
            step = await self.actor.next_step(state, loop)
        finally:
            self.actor.objective = original_objective

        # If the Actor returned finish/blocked, validate.
        if step.kind in ("finish", "blocked"):
            validation = await self._validate(sg, state)
            if validation.achieved:
                sg.status = "achieved"
                self.stats.sub_goals_achieved += 1
                self.stats.validations_passed += 1
                log_event(
                    log, 20, "sub-goal achieved",
                    sub_goal_id=sg.id, goal=sg.goal, reason=validation.reason,
                )
                # If this was the last sub-goal, finish.
                if self.plan.all_achieved:
                    return Step(
                        kind="finish",
                        action="noop",
                        args={},
                        reason="all sub-goals achieved",
                    )
                # Otherwise return a noop so the loop calls us again for the next.
                return Step(
                    kind="observe",
                    action="noop",
                    args={},
                    reason=f"sub-goal {sg.id} achieved; advancing to next",
                )
            else:
                self.stats.validations_failed += 1
                if sg.attempts >= sg.max_attempts:
                    sg.status = "failed"
                    self.stats.sub_goals_failed += 1
                    log_event(
                        log, 30, "sub-goal failed",
                        sub_goal_id=sg.id, goal=sg.goal, attempts=sg.attempts,
                    )
                    # Re-plan the remaining sub-goals (Reflexion).
                    self.stats.reflexion_replans += 1
                    self.plan = await self._make_plan(state, previous_failures=[sg])
                else:
                    log_event(
                        log, 30, "sub-goal validation failed, retrying",
                        sub_goal_id=sg.id, attempt=sg.attempts, reason=validation.reason,
                    )
                    sg.status = "pending"  # retry
                return Step(
                    kind="observe",
                    action="noop",
                    args={},
                    reason=f"sub-goal {sg.id} validation: {validation.reason}",
                )

        # Otherwise, return the Actor's step (navigate/click/fill/etc.).
        return step

    async def _make_plan(
        self, state: BrowserState, *, previous_failures: list[SubGoal] | None = None
    ) -> Plan | None:
        """Call the Planner LLM to decompose the objective into sub-goals."""
        if not self.client.available():
            log.info("PAV planner unavailable; using deterministic fallback")
            return None
        self.stats.plans_made += 1
        # Build the plan prompt.
        state_view = {
            "objective": self.objective,
            "current_url": state.current_url,
            "title": state.title,
            "page_type": state.page_type,
            "technologies": list(state.technologies),
            "routes": list(state.routes),
            "forms": [{"action": f.action, "has_password": f.has_password} for f in state.forms[:4]],
            "interactive_elements": [
                {"role": e.role, "name": e.name} for e in state.interactive_elements[:15]
            ],
        }
        user_prompt = (
            f"OBJECTIVE:\n{self.objective}\n\n"
            f"CURRENT STATE:\n{json.dumps(state_view, indent=2)}\n\n"
        )
        if previous_failures:
            user_prompt += (
                f"PREVIOUS FAILURES (avoid these approaches):\n"
                + json.dumps(
                    [{"goal": sg.goal, "reason": sg.success_criteria} for sg in previous_failures],
                    indent=2,
                )
                + "\n\n"
            )
        user_prompt += (
            f"Decompose into at most {self.max_sub_goals} sub-goals. "
            "Return a JSON array of {id, goal, success_criteria}."
        )
        try:
            response = await self.client.complete(
                system=PLANNER_SYSTEM_PROMPT,
                user=user_prompt,
                timeout_s=self.timeout_s,
                cancellation=self.cancellation,
            )
        except (TaskCancelledError, LLMTimeoutError, LLMUnavailableError, Exception) as e:  # noqa: BLE001
            log.warning(
                "PAV planner LLM call failed; using deterministic fallback",
                extra={"event": "pav.planner.fail", "err": str(e)},
            )
            return None

        # Parse the plan.
        try:
            parsed = parse_llm_response(response.content)
            if not isinstance(parsed, list):
                log.warning("PAV planner returned non-list; falling back")
                return None
            sub_goals: list[SubGoal] = []
            for i, sg in enumerate(parsed[: self.max_sub_goals], 1):
                if not isinstance(sg, dict):
                    continue
                sub_goals.append(
                    SubGoal(
                        id=int(sg.get("id", i)),
                        goal=str(sg.get("goal", "")),
                        success_criteria=str(sg.get("success_criteria", "")),
                    )
                )
            if not sub_goals:
                return None
            self.stats.sub_goals_total = len(sub_goals)
            log_event(
                log, 20, "plan made",
                sub_goals=len(sub_goals),
                goals=[sg.goal for sg in sub_goals],
            )
            return Plan(sub_goals=sub_goals)
        except (ActionValidationError, Exception) as e:  # noqa: BLE001
            log.warning(
                "PAV planner response unparseable; falling back",
                extra={"event": "pav.planner.parse.fail", "err": str(e)},
            )
            return None

    async def _validate(self, sg: SubGoal, state: BrowserState) -> ValidationResult:
        """Call the Validator LLM to check if the sub-goal was achieved."""
        if not self.client.available():
            # No LLM → assume achieved (let the Actor decide via finish).
            return ValidationResult(achieved=True, reason="no LLM validator; assuming achieved")
        state_view = {
            "current_url": state.current_url,
            "title": state.title,
            "page_type": state.page_type,
            "technologies": list(state.technologies),
            "routes": list(state.routes),
            "forms": len(state.forms),
            "interactive_elements": len(state.interactive_elements),
            "recent_observations": list(state.observations)[-3:],
        }
        user_prompt = (
            f"SUB-GOAL:\n{sg.goal}\n\n"
            f"SUCCESS CRITERIA:\n{sg.success_criteria or '(implicit: the goal is met)'}\n\n"
            f"CURRENT STATE:\n{json.dumps(state_view, indent=2)}\n\n"
            "Was this sub-goal achieved? Respond with JSON: "
            "{\"achieved\": true/false, \"reason\": \"...\"}"
        )
        try:
            response = await self.client.complete(
                system=VALIDATOR_SYSTEM_PROMPT,
                user=user_prompt,
                timeout_s=self.timeout_s,
                cancellation=self.cancellation,
            )
        except (TaskCancelledError, LLMTimeoutError, LLMUnavailableError, Exception) as e:  # noqa: BLE001
            log.warning(
                "PAV validator LLM call failed; assuming achieved",
                extra={"event": "pav.validator.fail", "err": str(e)},
            )
            return ValidationResult(achieved=True, reason=f"validator unavailable: {e}")
        # Parse.
        try:
            parsed = parse_llm_response(response.content)
            if isinstance(parsed, dict):
                return ValidationResult(
                    achieved=bool(parsed.get("achieved", False)),
                    reason=str(parsed.get("reason", "")),
                )
        except (ActionValidationError, Exception) as e:  # noqa: BLE001
            log.warning(
                "PAV validator response unparseable; assuming achieved",
                extra={"event": "pav.validator.parse.fail", "err": str(e)},
            )
        return ValidationResult(achieved=True, reason="validator response unparseable")


__all__ = [
    "PlanActValidateOrchestrator",
    "PAVStats",
    "Plan",
    "SubGoal",
    "ValidationResult",
    "PLANNER_SYSTEM_PROMPT",
    "VALIDATOR_SYSTEM_PROMPT",
]
