"""Evaluation harness — WebArena/Mind2Web-style local task suite + judge.

Inspired by browser-use's Auto-Research and the WebArena/VisualWebArena
benchmarks. Provides:

1. **Task definitions** — a set of deterministic tasks against the local
   test site (tests/fixtures/local_site/). Each task has:
   - `id`, `target`, `objective`, `max_steps`, `expected_outcome`
2. **Task runner** — runs the BrowserAgent against each task, captures the
   report, and records timings.
3. **Judge** — scores whether the agent achieved the objective. Supports:
   - `deterministic` judge: checks URL/element conditions (fast, free)
   - `llm` judge: asks an LLM to grade (accurate, costs tokens)

Usage:
    from sera_browser.eval.harness import EvalHarness, DeterministicJudge
    harness = EvalHarness(judge=DeterministicJudge())
    results = await harness.run_all(browser_manager, settings, local_site_url)
    print(harness.summary(results))

The harness is intentionally framework-agnostic: it doesn't depend on pytest,
so it can be run standalone or as part of CI.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..logging import get_logger

log = get_logger("sera.eval.harness")


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


@dataclass
class EvalTask:
    """A single evaluation task."""
    id: str
    target: str  # base URL
    objective: str
    max_steps: int = 8
    timeout_s: int = 60
    # Expected outcome — used by the deterministic judge.
    expected_url_contains: str | None = None
    expected_title_contains: str | None = None
    expected_api_in_report: str | None = None
    expected_form_detected: bool = False
    expected_routes_count_min: int = 0
    # Free-text expected outcome for the LLM judge.
    expected_outcome: str = ""


@dataclass
class EvalResult:
    """The result of running one eval task."""
    task_id: str
    status: str  # passed | failed | error | timeout
    score: float  # 0.0 to 1.0
    detail: str = ""
    duration_ms: float = 0.0
    report: dict[str, Any] | None = None
    judge_reason: str = ""


# The default task suite — runs against the local test site.
DEFAULT_TASKS: list[EvalTask] = [
    EvalTask(
        id="nav-home",
        target="/",
        objective="Navigate to the home page and identify the site structure.",
        max_steps=5,
        expected_url_contains="/",
        expected_title_contains="SERA Local Test Site",
    ),
    EvalTask(
        id="discover-login",
        target="/",
        objective="Find the login page and identify the login form fields.",
        max_steps=8,
        expected_form_detected=True,
        expected_outcome="The report should show a form with a password field (the login form).",
    ),
    EvalTask(
        id="discover-api",
        target="/products",
        objective="Visit the products page and discover API endpoints.",
        max_steps=6,
        expected_api_in_report="/rest/items",
        expected_outcome="The report should list /rest/items as a discovered API endpoint.",
    ),
    EvalTask(
        id="route-discovery",
        target="/",
        objective="Explore the site and discover at least 3 internal routes.",
        max_steps=10,
        expected_routes_count_min=3,
    ),
    EvalTask(
        id="tech-detection",
        target="/",
        objective="Detect what technologies the site uses (jQuery is expected).",
        max_steps=5,
        expected_outcome="The report should list jQuery in the technology array.",
    ),
]


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------


class DeterministicJudge:
    """Scores tasks by checking expected conditions in the report.

    Fast, free, and deterministic. Use for CI. For subjective tasks
    (e.g. "describe the page"), use the LLM judge.
    """

    def judge(self, task: EvalTask, report: dict[str, Any]) -> EvalResult:
        score = 0.0
        checks: list[str] = []
        total_checks = 0

        if task.expected_url_contains:
            total_checks += 1
            url = report.get("target", "") or ""
            if task.expected_url_contains in url:
                score += 1.0
                checks.append(f"url contains {task.expected_url_contains!r} OK")
            else:
                checks.append(f"url contains {task.expected_url_contains!r} FAIL")

        if task.expected_title_contains:
            total_checks += 1
            title = report.get("title", "") or ""
            if task.expected_title_contains.lower() in title.lower():
                score += 1.0
                checks.append(f"title contains {task.expected_title_contains!r} OK")
            else:
                checks.append(f"title contains {task.expected_title_contains!r} FAIL")

        if task.expected_api_in_report:
            total_checks += 1
            apis = report.get("apis", [])
            if any(task.expected_api_in_report in a for a in apis):
                score += 1.0
                checks.append(f"api {task.expected_api_in_report!r} found OK")
            else:
                checks.append(f"api {task.expected_api_in_report!r} not found FAIL")

        if task.expected_form_detected:
            total_checks += 1
            forms = report.get("forms", [])
            if forms:
                score += 1.0
                checks.append(f"forms detected ({len(forms)}) OK")
            else:
                checks.append("no forms detected FAIL")

        if task.expected_routes_count_min > 0:
            total_checks += 1
            routes = report.get("routes", [])
            if len(routes) >= task.expected_routes_count_min:
                score += 1.0
                checks.append(f"routes={len(routes)} >= {task.expected_routes_count_min} OK")
            else:
                checks.append(f"routes={len(routes)} < {task.expected_routes_count_min} FAIL")

        # Normalize score.
        final_score = score / total_checks if total_checks > 0 else (
            1.0 if report.get("status") == "completed" else 0.0
        )
        status = "passed" if final_score >= 0.6 else "failed"
        return EvalResult(
            task_id=task.id,
            status=status,
            score=final_score,
            detail="; ".join(checks),
            report=report,
            judge_reason=f"deterministic judge: {len(checks)} checks, score={final_score:.2f}",
        )


class LLMJudge:
    """Scores tasks by asking an LLM to grade the report against the objective.

    Accurate but costs tokens. Use for final evaluation, not CI.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def judge(self, task: EvalTask, report: dict[str, Any]) -> EvalResult:
        if not self.client.available():
            # Fall back to deterministic if LLM is unavailable.
            return DeterministicJudge().judge(task, report)
        prompt = self._build_prompt(task, report)
        try:
            from ..cancel import CancellationManager

            response = await self.client.complete(
                system="You are an evaluation judge. Score the agent's report.",
                user=prompt,
                timeout_s=30.0,
                cancellation=CancellationManager(task_id="judge"),
            )
            parsed = LLMJudge._parse_score(response.content)
            status = "passed" if parsed["score"] >= 0.6 else "failed"
            return EvalResult(
                task_id=task.id,
                status=status,
                score=parsed["score"],
                detail=parsed["reason"],
                report=report,
                judge_reason=f"llm judge: {parsed['reason'][:200]}",
            )
        except Exception as e:  # noqa: BLE001
            return EvalResult(
                task_id=task.id,
                status="error",
                score=0.0,
                detail=f"judge error: {e}",
                report=report,
            )

    @staticmethod
    def _build_prompt(task: EvalTask, report: dict[str, Any]) -> str:
        report_summary = {
            "status": report.get("status"),
            "target": report.get("target"),
            "title": report.get("title"),
            "routes": report.get("routes", []),
            "apis": report.get("apis", []),
            "forms": len(report.get("forms", [])),
            "technology": report.get("technology", []),
            "summary": report.get("summary", ""),
        }
        return (
            f"OBJECTIVE: {task.objective}\n\n"
            f"EXPECTED OUTCOME: {task.expected_outcome or '(see objective)'}\n\n"
            f"AGENT REPORT SUMMARY:\n{json.dumps(report_summary, indent=2)}\n\n"
            "Score the agent from 0.0 (failed) to 1.0 (fully succeeded). "
            "Respond with JSON: {\"score\": 0.0, \"reason\": \"...\"}"
        )

    @staticmethod
    def _parse_score(raw: str) -> dict[str, Any]:
        try:
            import json as _json

            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                data = _json.loads(text[start : end + 1])
                return {"score": float(data.get("score", 0.0)), "reason": str(data.get("reason", ""))}
        except Exception:  # noqa: BLE001
            pass
        return {"score": 0.0, "reason": "failed to parse judge response"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class EvalHarness:
    """Runs a suite of eval tasks against the BrowserAgent.

    Usage:
        harness = EvalHarness(judge=DeterministicJudge())
        results = await harness.run_all(browser_manager, settings, local_site_url)
        print(harness.summary(results))
    """
    judge: Any  # DeterministicJudge | LLMJudge
    tasks: list[EvalTask] = field(default_factory=lambda: list(DEFAULT_TASKS))

    async def run_all(
        self,
        browser_manager: Any,
        settings: Any,
        base_url: str,
        *,
        tasks: list[EvalTask] | None = None,
    ) -> list[EvalResult]:
        """Run all tasks sequentially. Returns results."""
        results: list[EvalResult] = []
        for task in (tasks or self.tasks):
            result = await self.run_one(browser_manager, settings, base_url, task)
            results.append(result)
        return results

    async def run_one(
        self,
        browser_manager: Any,
        settings: Any,
        base_url: str,
        task: EvalTask,
    ) -> EvalResult:
        """Run a single eval task. Returns the result."""
        from ..cancel import CancellationManager
        from ..models import TaskCreate
        from ..session.manager import SessionManager

        target = base_url.rstrip("/") + task.target
        log.info(
            f"running eval task {task.id}",
            extra={"event": "eval.task.start", "task_id": task.id, "target": target},
        )
        started = time.perf_counter()
        sm = SessionManager(settings=settings, browser_manager=browser_manager)
        req = TaskCreate(
            target=target,
            task=task.objective,
            browser_mode="headless",
            timeout=task.timeout_s,
            local_lab=True,
            allowed_targets=["127.0.0.1", "localhost"],
            max_steps=task.max_steps,
        )
        rec = await sm.create(req)
        # Wait for completion.
        deadline = time.time() + task.timeout_s + 10
        while rec.task is not None and not rec.task.done():
            if time.time() > deadline:
                break
            await asyncio.sleep(0.5)
        elapsed = (time.perf_counter() - started) * 1000.0
        if rec.report is None:
            return EvalResult(
                task_id=task.id,
                status="error",
                score=0.0,
                detail=f"no report: {rec.error}",
                duration_ms=elapsed,
            )
        report_dict = rec.report.model_dump(mode="json")
        # Judge.
        if isinstance(self.judge, LLMJudge):
            result = await self.judge.judge(task, report_dict)
        else:
            result = self.judge.judge(task, report_dict)
        result.duration_ms = elapsed
        log.info(
            f"eval task {task.id}: {result.status} score={result.score:.2f}",
            extra={
                "event": "eval.task.done",
                "task_id": task.id,
                "status": result.status,
                "score": result.score,
                "ms": round(elapsed, 1),
            },
        )
        return result

    @staticmethod
    def summary(results: list[EvalResult]) -> dict[str, Any]:
        """Aggregate results into a summary dict."""
        total = len(results)
        passed = sum(1 for r in results if r.status == "passed")
        failed = sum(1 for r in results if r.status == "failed")
        errors = sum(1 for r in results if r.status == "error")
        avg_score = sum(r.score for r in results) / total if total else 0.0
        total_ms = sum(r.duration_ms for r in results)
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": round(passed / total, 3) if total else 0.0,
            "avg_score": round(avg_score, 3),
            "total_duration_ms": round(total_ms, 1),
            "tasks": [
                {
                    "id": r.task_id,
                    "status": r.status,
                    "score": round(r.score, 2),
                    "detail": r.detail[:200],
                    "duration_ms": round(r.duration_ms, 1),
                }
                for r in results
            ],
        }


__all__ = [
    "EvalTask",
    "EvalResult",
    "EvalHarness",
    "DeterministicJudge",
    "LLMJudge",
    "DEFAULT_TASKS",
]
