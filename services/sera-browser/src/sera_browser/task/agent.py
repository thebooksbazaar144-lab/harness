"""BrowserAgent — the orchestrator.

Core flow:
    Task
    → create isolated browser session
    → navigate
    → observe
    → update state
    → reason
    → act
    → observe again
    → recover from failures/loops
    → finish
    → return structured report
    → cleanup

The agent decides which browser actions are necessary based on the objective.
The orchestrator does NOT micromanage every browser action — that's the
planner's job.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from ..browser.driver import PlaywrightDriver
from ..browser.inspector import Inspector
from ..browser.manager import BrowserManager
from ..browser.screenshots import ScreenshotLayer
from ..browser.storage import StorageManager
from ..cancel import CancellationManager, TaskCancelledError
from ..challenge.detector import ChallengeDetector
from ..config import Settings
from ..logging import get_logger, log_event
from ..models import AgentReport, BrowserState
from ..net.recorder import NetworkRecorder
from ..security import validate_target
from ..state.state import (
    mark_authenticated,
    merge_challenges,
    merge_network,
    merge_storage,
    update_from_snapshot,
)
from .app_graph import ApplicationGraph
from .auth_state import AuthStateMachine
from .coverage import CoverageEngine
from .evidence import EvidenceLog
from .executor import ActionExecutor
from .llm_client import LLMClient, LLMConfig, make_llm_client
from .llm_planner import LLMPlannerStats, LLMTaskPlanner
from .loop import LoopRecoveryManager
from .planner import Step, StepFeedback, TaskPlanner
from .report import ReportBuilder

log = get_logger("sera.task.agent")


@dataclass
class TaskConfig:
    task_id: str
    target: str
    objective: str
    browser_mode: str | None
    timeout_s: int
    local_lab: bool = False
    allowed_targets: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    auth: dict[str, Any] | None = None
    max_steps: int | None = None
    allow_js_eval: bool | None = None


@dataclass
class TaskResult:
    task_id: str
    report: AgentReport | None
    error: str | None


class BrowserAgent:
    def __init__(
        self,
        settings: Settings,
        browser_manager: BrowserManager,
        *,
        on_state: Callable[[BrowserState], None] | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.browser_manager = browser_manager
        self._on_state = on_state
        # If llm_client is None but the provider is "zai", we lazily construct
        # a ZaiLLMClient on first run.
        self._llm_client = llm_client
        # Real-view browser viewer (set per-task when enable_viewer=True).
        self._viewer = None
        # Technology probe runs at most once per task (see _observe).
        self._tech_probed = False

    async def run(self, cfg: TaskConfig, cancellation: CancellationManager) -> TaskResult:
        """Run the full autonomous flow. Always cleans up before returning."""
        started_at = time.time()
        deadline = started_at + cfg.timeout_s

        # Resolve effective settings (per-task overrides).
        settings = self._apply_overrides(cfg)

        # Validate target (SSRF + allowlist).
        try:
            validate_target(cfg.target, settings)
        except PermissionError as e:
            log.warning(
                "target rejected",
                extra={"event": "agent.target.rejected", "target": cfg.target, "err": str(e)},
            )
            return TaskResult(
                task_id=cfg.task_id,
                report=None,
                error=f"target rejected: {e}",
            )

        # Launch browser if needed.
        try:
            handle = await self.browser_manager.launch(mode=cfg.browser_mode)
        except PlaywrightError as e:
            log.error("browser launch failed", extra={"event": "agent.launch.fail", "err": str(e)})
            return TaskResult(task_id=cfg.task_id, report=None, error=f"launch failed: {e}")

        effective_mode = handle.mode

        # Per-task session dir.
        session_dir = self.settings.session_root / cfg.task_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # State + recorder.
        state = BrowserState(task_id=cfg.task_id, target=cfg.target)
        recorder = NetworkRecorder(settings=settings, cancellation=cancellation)

        # Open an isolated context.
        cm = await self.browser_manager.new_context_manager(cancellation)
        extra_headers = None
        storage_state_path = None
        if cfg.auth:
            extra_headers = cfg.auth.get("headers")
            ss = cfg.auth.get("storage_state")
            if isinstance(ss, str):
                storage_state_path = Path(ss)

        try:
            page = await cm.open(
                handle.browser,
                storage_state_path=storage_state_path,
                extra_headers=extra_headers,
            )
        except PlaywrightError as e:
            log.error("context open failed", extra={"event": "agent.ctx.fail", "err": str(e)})
            await cm.force_close()
            return TaskResult(task_id=cfg.task_id, report=None, error=f"context open failed: {e}")

        # Wire network + page event handlers.
        recorder.attach(page)
        driver = PlaywrightDriver(settings=settings, cancellation=cancellation)
        self._wire_page_events(page, state, cancellation)

        # Real-view browser viewer (human-takeoverable, for SERA chat UI).
        viewer = None
        if getattr(settings, "enable_viewer", False):
            from ..browser.viewer import BrowserViewer
            cdp_port = getattr(handle, "cdp_port", None)
            viewer = BrowserViewer(
                task_id=cfg.task_id,
                session_dir=session_dir,
                screenshot_quality=getattr(settings, "viewer_screenshot_quality", 60),
                screenshot_max_width=getattr(settings, "viewer_screenshot_max_width", 1280),
                stream_screenshots=getattr(settings, "viewer_stream_screenshots", True),
            )
            viewer.start(page, cdp_port=cdp_port)
            # Store on the session manager so the API can access it.
            self._viewer = viewer
            log.info(
                "real-view browser viewer enabled",
                extra={
                    "event": "viewer.enabled",
                    "task_id": cfg.task_id,
                    "cdp_port": cdp_port,
                    "cdp_url": viewer.cdp_url,
                },
            )

        # Subsystems.
        # v4: THE ElementRegistry — single authoritative ref namespace.
        from ..browser.element_registry import ElementRegistry

        registry = ElementRegistry()
        inspector = Inspector(driver=driver, registry=registry)
        screenshots = ScreenshotLayer(session_dir / "screenshots", cancellation)
        storage = StorageManager(settings=settings, context_manager=cm, cancellation=cancellation)
        # v4: coverage engine (exploration integrity owner) + auth state
        # machine (evidence-based auth) + app graph + evidence log.
        coverage = CoverageEngine()
        auth_machine = AuthStateMachine()
        app_graph = ApplicationGraph()
        evidence_log = EvidenceLog(session_dir)
        # Multi-tab manager (optional, enabled by default).
        tab_manager = None
        if settings.enable_multi_tab:
            from ..browser.tabs import TabManager
            tab_manager = TabManager(primary_page=page)
        # History + Replay manager (dsh-browser inspired).
        from .history import HistoryManager
        history_manager = HistoryManager()
        # Soft action allowlist (dsh-browser inspired).
        from .soft_restrict import SoftRestrict
        soft_restrict = SoftRestrict()
        executor = ActionExecutor(
            driver, inspector, settings, cancellation,
            tab_manager=tab_manager,
            registry=registry,
            recorder=recorder,
        )
        # Wire dsh-browser-inspired subsystems into the executor.
        executor._history_manager = history_manager  # noqa: SLF001
        executor._soft_restrict = soft_restrict  # noqa: SLF001
        executor._storage_manager = storage  # noqa: SLF001
        loop = LoopRecoveryManager(
            tool_repeat_limit=settings.tool_repeat_limit,
            semantic_loop_limit=settings.semantic_loop_limit,
            failure_block_limit=getattr(settings, "failure_block_limit", 2),
        )
        coverage.attach_loop(loop)
        # Memory manager (optional, enabled by default).
        memory = None
        if settings.enable_memory:
            from .memory import MemoryManager
            memory = MemoryManager(max_full_steps=settings.memory_max_full_steps)
        # Safety guard (always on, configurable policy).
        from .safety import SafetyConfig, SafetyGuard
        safety_guard = SafetyGuard(SafetyConfig(
            action_policy=settings.safety_action_policy,
            block_dangerous=settings.safety_block_dangerous,
        ))
        # Observability (traces + action log).
        from .observability import Observability
        obs = Observability(
            task_id=cfg.task_id,
            session_dir=session_dir,
            enable_tracing=settings.enable_tracing,
            enable_action_log=settings.enable_action_log,
        )
        await obs.start_tracing(cm.context)  # type: ignore[arg-type]

        deterministic_planner = TaskPlanner(settings, coverage=coverage)
        # v4: credentials for the (single, evidence-verified) login attempt
        # flow. The planner reads them via settings._task_auth; the auth
        # state machine — not the attempt — decides authentication.
        settings._task_auth = cfg.auth if isinstance(cfg.auth, dict) else None  # type: ignore[attr-defined]
        planner = self._build_planner(
            settings, cancellation, cfg.objective, deterministic_planner
        )
        # v4: LLM planner gets the same coverage engine (unexplored-target
        # hints) + registry-aware state view.
        if isinstance(planner, LLMTaskPlanner):
            planner.attach_coverage(coverage)
            planner.attach_registry(registry)
        else:
            actor = getattr(planner, "actor", None)
            if isinstance(actor, LLMTaskPlanner):
                actor.attach_coverage(coverage)
                actor.attach_registry(registry)
        # If the planner is LLM-backed (directly or via PAV wrapper), wire
        # all the advanced subsystems into the inner LLMTaskPlanner.
        inner_llm: LLMTaskPlanner | None = None
        if isinstance(planner, LLMTaskPlanner):
            inner_llm = planner
        else:
            # PAV orchestrator wraps an LLMTaskPlanner as `actor`.
            actor = getattr(planner, "actor", None)
            if isinstance(actor, LLMTaskPlanner):
                inner_llm = actor
        if inner_llm is not None:
            inner_llm._screenshot_provider = lambda: screenshots.capture_bytes(page)  # noqa: SLF001
            if memory is not None:
                inner_llm._memory = memory  # noqa: SLF001
            inner_llm._safety_guard = safety_guard  # noqa: SLF001
            inner_llm._tab_manager = tab_manager  # noqa: SLF001
        llm_stats: LLMPlannerStats | None = (
            inner_llm.stats if inner_llm is not None else None
        )
        # v4: runtime backend selection (BrowserRuntime abstraction).
        # The agent execution core runs on LocalPlaywrightRuntime; the
        # kernel backend is available via create_runtime for SERA's engine
        # and agent-level wiring once the kernel service contract is live.
        from ..runtime import select_backend

        backend_name = select_backend(settings)

        challenge_detector = ChallengeDetector()

        status: str = "running"
        error: str | None = None
        summary: str = ""

        try:
            # Authenticated storage state save on shutdown, if we logged in
            # (decided by the auth STATE MACHINE, never by an attempt).
            save_state_after = False

            step_count = 0
            max_steps = cfg.max_steps or settings.max_steps
            last_feedback: StepFeedback | None = None

            while step_count < max_steps:
                cancellation.check()
                if time.time() > deadline:
                    status = "timeout"
                    summary = "task deadline exceeded"
                    break

                # Check for human takeover (real-view viewer).
                if viewer is not None and viewer.is_takeover_active:
                    log.info(
                        "agent pausing for human takeover",
                        extra={"event": "viewer.takeover.pause", "task_id": cfg.task_id},
                    )
                    resumed = await viewer.wait_for_resume(
                        timeout_s=getattr(settings, "viewer_takeover_timeout_s", 300),
                        cancellation=cancellation,
                    )
                    if not resumed:
                        log.warning("human takeover timed out, resuming", extra={"event": "viewer.takeover.timeout"})
                    # Re-observe after the human finished.
                    await self._observe(
                        page, state, driver, inspector, challenge_detector,
                        loop, screenshots, cfg, settings,
                    )
                    merge_network(state, recorder)

                # v4: feed the structured result of the LAST step to the
                # planner BEFORE it decides the next one. This is the
                # restored feedback channel: the planner sees action
                # outcomes, failures, navigation changes, and discoveries.
                if last_feedback is not None:
                    try:
                        planner.receive_feedback(last_feedback)  # type: ignore[attr-defined]
                    except AttributeError:
                        pass

                # Reason
                step = await planner.next_step(state, loop)
                log_event(
                    log,
                    20,
                    "planner step",
                    task_id=cfg.task_id,
                    planner="llm" if isinstance(planner, LLMTaskPlanner) else "deterministic",
                    step=step.kind,
                    action=step.action,
                    reason=step.reason,
                )

                if step.kind == "finish":
                    status = "completed"
                    summary = step.reason
                    break
                if step.kind == "blocked":
                    status = "blocked"
                    summary = step.reason
                    break

                # Act
                if step.action == "noop":
                    # Observation step.
                    obs_summary = await self._observe(
                        page,
                        state,
                        driver,
                        inspector,
                        challenge_detector,
                        loop,
                        screenshots,
                        cfg,
                        settings,
                    )
                    merge_network(state, recorder)
                    state.add_observation(step.reason)
                    last_feedback = StepFeedback(
                        step=step_count + 1,
                        action="noop",
                        ok=True,
                        detail=step.reason,
                        new_refs=getattr(obs_summary, "new_refs", None) or [],
                        lost_refs=getattr(obs_summary, "lost_refs", None) or [],
                    )
                else:
                    # Start an observability span for this action.
                    span = obs.start_span(step.action, step.args, snapshot_version=registry.version)
                    res = await executor.execute(page, state, loop, step.action, **step.args)
                    # Record in history (dsh-browser inspired).
                    history_manager.record(
                        step.action, step.args, res.ok,
                        result=res.detail if res.ok else None,
                        error=res.detail if not res.ok else None,
                    )
                    # Record in memory (v4: enriched with failure kind and
                    # structured data availability).
                    if memory is not None:
                        memory.record_step(
                            action=step.action,
                            args=step.args,
                            result="ok" if res.ok else "failed",
                            detail=res.detail,
                            observation=step.reason,
                            url=state.current_url or "",
                        )
                    # Finish the span.
                    obs.finish_span(
                        span,
                        status="ok" if res.ok else "failed",
                        detail=res.detail,
                    )
                    # Handle update_memory action specially.
                    if step.action == "update_memory" and memory is not None:
                        memory.update_long_term_memory(step.args.get("memory", ""))

                    if not res.ok:
                        log.warning(
                            "action failed",
                            extra={
                                "event": "agent.action.fail",
                                "action": step.action,
                                "detail": res.detail[:200],
                                "failure": res.failure.kind if res.failure else None,
                            },
                        )

                    # v4: exactly ONE re-observe per action (the old flow
                    # observed twice on failure — a snapshot storm).
                    routes_before_observe = set(state.routes)
                    obs_summary = await self._observe(
                        page,
                        state,
                        driver,
                        inspector,
                        challenge_detector,
                        loop,
                        screenshots,
                        cfg,
                        settings,
                    )
                    merge_network(state, recorder)

                    # v4: evidence-based auth evaluation. A login attempt
                    # is NOT authentication — the state machine decides
                    # from observable postconditions only.
                    login_just_attempted = step.kind == "attempt_safe_login"
                    auth_machine.evaluate(state, login_just_attempted=login_just_attempted)
                    coverage.record_auth_state(auth_machine.state)
                    if auth_machine.state == "AUTHENTICATED" and not save_state_after:
                        save_state_after = True
                        state.add_observation(
                            "auth: AUTHENTICATED confirmed by "
                            f"{auth_machine.last_evidence.positive_count() if auth_machine.last_evidence else 0} "
                            "observable postconditions"
                        )

                    # v4: app-graph correlation (action → attributed
                    # network requests → route transition).
                    app_graph.record_action(
                        step_count + 1,
                        step.action,
                        res.ref,
                        res.url_before,
                        res.url_after,
                        res.attributed_requests,
                        ok=res.ok,
                    )
                    for wf in app_graph.workflows()[-1:]:
                        coverage.record_workflow(wf)
                    coverage.ingest_network(
                        [
                            f"{r['method']} {r['url']}"
                            for r in res.attributed_requests
                            if r.get("resource_type") in ("xhr", "fetch") or r.get("graphql")
                        ]
                    )

                    # v4: per-step structured evidence.
                    evidence_log.record(
                        step_count + 1,
                        step.action,
                        step.args,
                        ok=res.ok,
                        failure=res.failure.as_dict() if res.failure else None,
                        detail=res.detail,
                        ref=res.ref,
                        url_before=res.url_before,
                        url_after=res.url_after,
                        attributed_requests=res.attributed_requests,
                        screenshot=state.screenshots[-1] if state.screenshots else None,
                        data_keys=list(res.data.keys()) if res.data else [],
                        duration_ms=res.duration_ms,
                    )

                    # v4: build the structured feedback for the planner.
                    new_routes = [
                        r
                        for r in (getattr(obs_summary, "routes", None) or [])
                        if r not in routes_before_observe
                    ]
                    last_feedback = StepFeedback(
                        step=step_count + 1,
                        action=step.action,
                        ok=res.ok,
                        ref=res.ref,
                        failure_kind=res.failure.kind if res.failure else None,
                        detail=res.detail,
                        url_before=res.url_before,
                        url_after=res.url_after,
                        route_changed=res.url_before != res.url_after,
                        new_refs=getattr(obs_summary, "new_refs", None) or [],
                        lost_refs=getattr(obs_summary, "lost_refs", None) or [],
                        new_routes=new_routes[:8],
                        attributed_requests=res.attributed_requests[:12],
                        data_keys=list(res.data.keys()) if res.data else [],
                    )
                    # Restore the feedback as a readable observation
                    # (v3 starved the LLM of this: ≤200-char breadcrumbs).
                    state.add_observation(last_feedback.compact())

                    # v4: hard loop escape AFTER the action — abandons the
                    # current target (blocked set) so the planner MUST
                    # select a different unexplored target. Never a
                    # screenshot.
                    loop.record_url(state.current_url)
                    escape_signal = loop.escape(
                        state, current_ref=step.args.get("ref") or res.ref
                    )
                    if escape_signal is not None:
                        state.add_observation(
                            f"loop escape ({escape_signal}): target abandoned; "
                            "planner will select a different unexplored target"
                        )

                step_count += 1
                state.timings.total_steps = step_count

                # Broadcast step to viewer clients (real-view streaming).
                if viewer is not None:
                    try:
                        await viewer.broadcast_step(
                            page,
                            step_num=step_count,
                            action=step.action,
                            result="ok" if (step.action == "noop" or res.ok) else "failed",
                            detail=step.reason,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.debug("viewer broadcast failed", extra={"event": "viewer.broadcast.fail", "err": str(e)})

            if status == "running":
                # Out of steps.
                status = "completed"
                summary = "max_steps reached; coverage status: " + (
                    coverage.completion_reason(state) or "frontier not exhausted"
                )

            # Record LLM planner stats as an observation for SERA visibility.
            if llm_stats is not None:
                stats = llm_stats.as_dict()
                state.add_observation(
                    f"llm_planner: calls={stats['llm_calls']} "
                    f"succeeded={stats['llm_succeeded']} "
                    f"fallbacks={stats['fallback_to_deterministic']} "
                    f"invalid={stats['invalid_action']} "
                    f"vision_calls={stats['vision_calls']}"
                )
            # Record PAV orchestrator stats if active.
            from .pav import PlanActValidateOrchestrator
            if isinstance(planner, PlanActValidateOrchestrator):
                pav = planner.stats.as_dict()
                state.add_observation(
                    f"pav: plans={pav['plans_made']} sub_goals={pav['sub_goals_total']} "
                    f"achieved={pav['sub_goals_achieved']} failed={pav['sub_goals_failed']} "
                    f"replans={pav['reflexion_replans']}"
                )
            # Record memory stats.
            if memory is not None:
                mstats = memory.stats
                state.add_observation(
                    f"memory: steps={mstats['total_steps']} "
                    f"ltm_chars={mstats['long_term_memory_chars']} "
                    f"summary_chars={mstats['summary_chars']}"
                )
            # Record history stats (dsh-browser inspired).
            hstats = history_manager.stats
            state.add_observation(
                f"history: entries={hstats['total_entries']} "
                f"next_seq={hstats['next_seq']}"
            )
            # Record viewer stats (real-view browser).
            if viewer is not None:
                vstats = viewer.stats.as_dict()
                state.add_observation(
                    f"viewer: steps_streamed={vstats['steps_streamed']} "
                    f"screenshots={vstats['screenshots_streamed']} "
                    f"takeovers={vstats['takeovers_requested']} "
                    f"clients={vstats['clients_current']}"
                )
            # Record observability summary.
            obs_summary = obs.summary()
            if obs_summary["total_spans"] > 0:
                state.add_observation(
                    f"observability: spans={obs_summary['total_spans']} "
                    f"ok={obs_summary['ok']} failed={obs_summary['failed']} "
                    f"trace={obs_summary.get('trace_path') or 'disabled'}"
                )

            # Final storage + state merge.
            try:
                cookies = await storage.get_cookies(page)
                ls_keys, ss_keys = await storage.list_storage_keys(page)
                merge_storage(state, cookies, ls_keys, ss_keys)
            except Exception as e:  # noqa: BLE001
                log.debug("storage read failed", extra={"event": "agent.storage.fail", "err": str(e)})

            merge_network(state, recorder)

            if save_state_after:
                try:
                    await storage.save_state(session_dir / "storage_state.json")
                except Exception as e:  # noqa: BLE001
                    log.debug("save storage state failed", extra={"event": "agent.save_state.fail", "err": str(e)})

        except TaskCancelledError as e:
            status = "cancelled"
            error = str(e)
            summary = f"cancelled: {e.reason}"
            log.info("task cancelled", extra={"event": "agent.cancelled", "task_id": cfg.task_id, "reason": e.reason})
        except Exception as e:  # noqa: BLE001
            status = "failed"
            error = repr(e)
            summary = f"failed: {e}"
            log.exception("task failed", extra={"event": "agent.fail", "err": str(e)})
        finally:
            # Stop the viewer (disconnect WebSocket clients).
            if viewer is not None:
                try:
                    await viewer.stop()
                except Exception:  # noqa: BLE001
                    pass
            # Stop observability tracing + flush action log.
            try:
                if cm.context is not None:
                    await obs.stop_tracing(cm.context)  # type: ignore[arg-type]
                obs.flush()
            except Exception:  # noqa: BLE001
                pass
            # Always clean up the browser.
            await self._cleanup(cm, cancellation)

        finished_at = time.time()
        report = ReportBuilder.build(
            state,
            recorder,
            status=status,  # type: ignore[arg-type]
            task_id=cfg.task_id,
            started_at=started_at,
            finished_at=finished_at,
            browser_mode=effective_mode,
            error=error,
            summary=summary,
            coverage=coverage,
            evidence=evidence_log,
            app_graph=app_graph,
            auth_machine=auth_machine,
            loop=loop,
            registry=registry,
            session_dir=session_dir,
            browser_backend=backend_name,
        )
        return TaskResult(task_id=cfg.task_id, report=report, error=error)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_overrides(self, cfg: TaskConfig) -> Settings:
        """Return a settings copy with per-task overrides applied."""
        s = self.settings.model_copy(deep=True)
        if cfg.local_lab:
            s.local_lab = True
            # Merge in allowed_targets → allowed_private_targets.
            existing = set(s.allowed_private_targets_list)
            for t in cfg.allowed_targets:
                existing.add(t)
            s.allowed_private_targets = ",".join(sorted(existing))
        if cfg.allowed_domains:
            existing = set(s.allowed_domains_list)
            for d in cfg.allowed_domains:
                existing.add(d)
            s.allowed_domains = ",".join(sorted(existing))
        if cfg.max_steps:
            s.max_steps = cfg.max_steps
        if cfg.allow_js_eval is not None:
            s.allow_js_eval = cfg.allow_js_eval
        return s

    def _build_planner(
        self,
        settings: Settings,
        cancellation: CancellationManager,
        objective: str,
        deterministic: TaskPlanner,
    ):
        """Construct the active planner.

        Provider-agnostic: the LLM client is selected by `make_llm_client()`
        based on `settings.llm_provider` (`none` | `zai` | `openai`).
        The Browser Agent architecture does NOT hardcode any specific provider.

        - `SBA_LLM_PROVIDER=none` (default) → deterministic planner only.
        - `SBA_LLM_PROVIDER=zai` → `ZaiLLMClient` (z-ai CLI subprocess).
        - `SBA_LLM_PROVIDER=openai` → `OpenAICompatLLMClient` (OpenAI/Azure/
          OpenRouter/Together/vLLM/Ollama/...).
        - If an `llm_client` was injected (tests), use it directly.
        """
        if settings.llm_provider == "none":
            return deterministic

        # If a client was injected (e.g. MockLLMClient in tests), use it.
        if self._llm_client is not None:
            client = self._llm_client
        else:
            # Build via the factory.
            llm_config = settings.to_llm_config()
            client = make_llm_client(llm_config)
            self._llm_client = client

        llm_planner = LLMTaskPlanner(
            settings=settings,
            client=client,
            cancellation=cancellation,
            objective=objective,
            deterministic=deterministic,
            timeout_s=settings.llm_timeout_s,
        )

        # If the Plan-Act-Validate pattern is enabled, wrap the LLM planner
        # in the PAV orchestrator (Skyvern-style Planner/Actor/Validator).
        if settings.agent_pattern == "plan_act_validate":
            from .pav import PlanActValidateOrchestrator
            return PlanActValidateOrchestrator(
                settings=settings,
                client=client,
                cancellation=cancellation,
                objective=objective,
                actor=llm_planner,
                deterministic=deterministic,
                timeout_s=settings.llm_timeout_s,
            )
        return llm_planner

    async def _observe(
        self,
        page: Page,
        state: BrowserState,
        driver: PlaywrightDriver,
        inspector: Inspector,
        challenge_detector: ChallengeDetector,
        loop: LoopRecoveryManager,
        screenshots: ScreenshotLayer,
        cfg: TaskConfig,
        settings: Settings,
    ):
        """Observe: snapshot + registry + DOM-derived intelligence.

        v4: returns the SnapshotSummary so the caller can build structured
        feedback (new/lost refs, routes)."""
        # Snapshot + DOM-derived forms/links/routes.
        summary = await inspector.observe(page)
        update_from_snapshot(state, summary)
        loop.record_dom_hash(state.dom_hash)
        loop.record_url(state.current_url)

        # Technology detection (genuinely ONCE — a site with no detectable
        # tech markers must not re-probe forever; the planner's phase-2 exit
        # condition treats [] as "not yet probed").
        if not state.technologies and not self._tech_probed:
            self._tech_probed = True
            try:
                tech = await driver.detect_technologies(page)
                state.technologies = tech
            except Exception as e:  # noqa: BLE001
                log.debug("tech detect failed", extra={"event": "agent.tech.fail", "err": str(e)})

        # Challenge detection.
        try:
            ch = await challenge_detector.detect(page)
            merge_challenges(state, ch.challenges)
            # If a challenge requires human-in-loop and we don't have an
            # official test key, capture a screenshot as evidence and pause.
            needs_human = challenge_detector.requires_human(ch.challenges)
            if needs_human and not any(c.handled for c in ch.challenges):
                path = await screenshots.capture(page, label=f"challenge_{needs_human.type}")
                if path:
                    state.screenshots.append(str(path))
                state.add_observation(
                    f"challenge requires human-in-loop: {needs_human.type} "
                    f"(evidence: {needs_human.evidence})"
                )
        except Exception as e:  # noqa: BLE001
            log.debug("challenge detect failed", extra={"event": "agent.challenge.fail", "err": str(e)})

        # NetworkRecorder is updated continuously by event handlers wired in
        # `run()`. The recorder is merged into state at the end of each
        # loop iteration via `merge_network` in `run()`.
        if self._on_state is not None:
            try:
                self._on_state(state)
            except Exception as e:  # noqa: BLE001
                log.debug("on_state callback failed", extra={"event": "agent.on_state.fail", "err": str(e)})

    def _wire_page_events(
        self,
        page: Page,
        state: BrowserState,
        cancellation: CancellationManager,
    ) -> None:
        """Wire console/pageerror/download events directly into BrowserState.

        The NetworkRecorder has its own wiring for requests/responses/websockets.
        """
        from ..models import ConsoleMessage, DownloadRecord, PageError

        def _on_console(msg) -> None:  # type: ignore[no-untyped-def]
            if cancellation.cancelled:
                return
            try:
                state.console_errors.append(
                    ConsoleMessage(
                        type=msg.type,
                        text=msg.text,
                        url=msg.location.get("url") if hasattr(msg, "location") else None,
                        line=msg.location.get("lineNumber") if hasattr(msg, "location") else None,
                    )
                )
            except Exception:  # noqa: BLE001
                pass

        def _on_pageerror(err) -> None:  # type: ignore[no-untyped-def]
            if cancellation.cancelled:
                return
            try:
                state.page_errors.append(
                    PageError(message=str(err), url=page.url)
                )
            except Exception:  # noqa: BLE001
                pass

        def _on_download(dl) -> None:  # type: ignore[no-untyped-def]
            if cancellation.cancelled:
                return
            try:
                state.downloads.append(
                    DownloadRecord(
                        suggested_filename=dl.suggested_filename,
                        url=dl.url,
                    )
                )
            except Exception:  # noqa: BLE001
                pass

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("download", _on_download)

    async def _cleanup(self, cm, cancellation: CancellationManager) -> None:  # type: ignore[no-untyped-def]
        """Cooperative + force cleanup. Always runs.

        NOTE: We intentionally do NOT call BrowserManager._sweep_orphan_chromium
        here — that sweep kills ALL ms-playwright chromium processes and would
        break concurrent/sequential tasks sharing the same BrowserManager.
        The sweep is reserved for the API shutdown path (SessionManager.shutdown_all).
        """
        try:
            await asyncio.wait_for(cm.close(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        finally:
            try:
                await cm.force_close()
            except Exception:  # noqa: BLE001
                pass
