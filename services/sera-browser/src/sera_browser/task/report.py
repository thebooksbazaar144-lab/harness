"""ReportBuilder — deterministic structured report + machine-readable
BrowserRun intelligence package.

v4: the report gains additive coverage/evidence/app-graph/auth-state
sections (all optional kwargs — v3 call sites still work). The
intelligence package (Phase 15) is ALSO saved to the session directory as
``run_package.json`` so SERA never has to understand Playwright: it gets
application, routes, workflows, forms, endpoints, auth states, network
events, storage metadata, evidence, and coverage in one JSON blob.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from ..models import AgentReport, AuthenticationSummary, BrowserState, NetworkSummary

if TYPE_CHECKING:
    from .app_graph import ApplicationGraph
    from .auth_state import AuthStateMachine
    from .coverage import CoverageEngine
    from .evidence import EvidenceLog
    from .loop import LoopRecoveryManager

    from ..browser.element_registry import ElementRegistry

log = get_logger("sera.task.report")


class ReportBuilder:
    @staticmethod
    def build(
        state: BrowserState,
        recorder,
        *,
        status: str,
        task_id: str,
        started_at: float,
        finished_at: float,
        browser_mode: str | None,
        error: str | None = None,
        summary: str = "",
        coverage: "CoverageEngine | None" = None,
        evidence: "EvidenceLog | None" = None,
        app_graph: "ApplicationGraph | None" = None,
        auth_machine: "AuthStateMachine | None" = None,
        loop: "LoopRecoveryManager | None" = None,
        registry: "ElementRegistry | None" = None,
        session_dir: "Path | None" = None,
        browser_backend: str | None = None,
    ) -> AgentReport:  # type: ignore[no-untyped-def]
        # Authentication summary — v4: evidence-based. The v3 code inferred
        # "detected" from the mere PRESENCE of a password form; the auth
        # state machine's verdict is authoritative when available.
        password_form_seen = bool(state.forms and any(f.has_password for f in state.forms))
        auth_state_name = auth_machine.state if auth_machine is not None else (state.auth_state or "ANONYMOUS")
        auth = AuthenticationSummary(
            detected=password_form_seen or auth_state_name not in ("ANONYMOUS", None),
            type="form" if password_form_seen else None,
            login_url=state.current_url if state.page_type == "login" else None,
            roles_observed=([state.user_role] if state.user_role else []),
            storage_state_saved=bool(state.authenticated),
            csrf_observed=any(f.has_csrf for f in state.forms),
        )

        # Network summary
        net = NetworkSummary(
            **recorder.summary(),
        )

        # Forms
        forms = [
            {
                "ref": f.ref,
                "action": f.action,
                "method": f.method,
                "has_password": f.has_password,
                "has_csrf": f.has_csrf,
                "fields": [
                    {
                        "ref": ie.ref,
                        "tag": ie.tag,
                        "type": ie.type,
                        "name": ie.name,
                        "placeholder": ie.placeholder,
                    }
                    for ie in f.fields
                ],
            }
            for f in state.forms
        ]

        # Challenges
        challenges = [c.model_dump(mode="json") for c in state.challenges]

        # Observations
        observations = list(state.observations)

        # Summary text
        if not summary:
            summary = ReportBuilder._summarize(state, recorder, status)

        # --- v4 sections -------------------------------------------------
        # Workflows: REAL observed workflows from the app graph (action →
        # endpoints → route change) when available; v3 heuristic labels
        # only as fallback.
        if app_graph is not None:
            workflow_strings = [w["workflow"] for w in app_graph.workflows()]
        else:
            workflow_strings = ReportBuilder._workflows(state, recorder)

        coverage_summary = coverage.summary() if coverage is not None else None
        evidence_list = evidence.to_list() if evidence is not None else []
        app_graph_dict = app_graph.to_dict() if app_graph is not None else None
        auth_states = auth_machine.summary() if auth_machine is not None else None
        blocked_targets = sorted(
            set(loop.blocked_refs if loop is not None else set())
            | set(loop.blocked_routes if loop is not None else set())
        )
        registry_stats = registry.stats() if registry is not None else None

        report = AgentReport(
            status=status,  # type: ignore[arg-type]
            target=state.target,
            title=state.title,
            technology=list(state.technologies),
            routes=list(state.routes),
            apis=recorder.api_endpoints(),
            forms=forms,
            authentication=auth,
            workflows=workflow_strings,
            network=net,
            screenshots=list(state.screenshots),
            console_errors=[m.text for m in state.console_errors],
            challenges=challenges,
            observations=observations,
            summary=summary,
            task_id=task_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(finished_at - started_at) * 1000.0,
            steps=state.timings.total_steps,
            browser_mode=browser_mode,
            error=error,
            # v4 additive sections
            coverage=coverage_summary,
            evidence=evidence_list,
            app_graph=app_graph_dict,
            auth_states=auth_states,
            blocked_targets=blocked_targets,
            registry_stats=registry_stats,
            browser_backend=browser_backend,
        )

        # v4 Phase 15: machine-readable BrowserRun intelligence package,
        # written next to the report so SERA consumes application
        # intelligence without any Playwright knowledge.
        ReportBuilder._save_run_package(
            state, recorder, report, coverage, evidence, app_graph, auth_machine, task_id,
            session_dir=session_dir,
        )
        return report

    @staticmethod
    def _save_run_package(
        state: BrowserState,
        recorder,
        report: AgentReport,
        coverage: "CoverageEngine | None",
        evidence: "EvidenceLog | None",
        app_graph: "ApplicationGraph | None",
        auth_machine: "AuthStateMachine | None",
        task_id: str,
        session_dir: "Path | None" = None,
    ) -> None:
        """Write run_package.json into the session directory."""
        try:
            if session_dir is None:
                from ..config import get_settings

                session_dir = Path(get_settings().session_root) / task_id
            session_dir = Path(session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            package = {
                "schema": "sera-browser.run-package/v1",
                "task_id": task_id,
                "target": state.target,
                "status": report.status,
                "application": {
                    "title": state.title,
                    "technologies": list(state.technologies),
                    "routes": list(state.routes),
                    "forms": [
                        {
                            "action": f.action,
                            "method": f.method,
                            "has_password": f.has_password,
                            "has_csrf": f.has_csrf,
                            "field_count": len(f.fields),
                        }
                        for f in state.forms
                    ],
                },
                "workflows": app_graph.workflows() if app_graph else [],
                "endpoints": recorder.api_endpoints(),
                "auth_states": auth_machine.summary() if auth_machine else {"state": "ANONYMOUS"},
                "network": recorder.summary(),
                "storage": {
                    "cookie_count": len(state.cookies or []),
                    "cookie_names": [c.get("name") for c in (state.cookies or [])][:60],
                    "local_storage_keys": list(state.local_storage_keys or [])[:60],
                    "session_storage_keys": list(state.session_storage_keys or [])[:60],
                },
                "evidence": evidence.to_list() if evidence else [],
                "coverage": coverage.summary() if coverage else None,
                "blocked_targets": report.blocked_targets,
                "duration_ms": report.duration_ms,
                "steps": report.steps,
            }
            path = session_dir / "run_package.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(package, f, default=str, indent=1)
            log.info(
                "run package saved",
                extra={"event": "report.run_package.saved", "path": str(path)},
            )
        except Exception as e:  # noqa: BLE001 — packaging must never fail the report
            log.debug(
                "run package save failed",
                extra={"event": "report.run_package.fail", "err": str(e)},
            )

    @staticmethod
    def _workflows(state: BrowserState, recorder) -> list[str]:  # type: ignore[no-untyped-def]
        """Heuristic workflow labels."""
        wf: list[str] = []
        if state.page_type == "spa":
            wf.append("spa-routing")
        if any(f.has_password for f in state.forms):
            wf.append("authentication-form")
        if recorder.graphql_operations():
            wf.append("graphql-api")
        if recorder.websocket_endpoints():
            wf.append("websocket-usage")
        if any("/api/" in e or "/rest/" in e for e in recorder.api_endpoints()):
            wf.append("rest-api")
        return wf

    @staticmethod
    def _summarize(state: BrowserState, recorder, status: str) -> str:  # type: ignore[no-untyped-def]
        parts: list[str] = []
        parts.append(f"Status: {status}.")
        parts.append(f"Target {state.target} analyzed.")
        if state.title:
            parts.append(f"Page title: {state.title!r}.")
        if state.technologies:
            parts.append("Technologies detected: " + ", ".join(state.technologies) + ".")
        if state.routes:
            parts.append(f"Discovered {len(state.routes)} internal route(s).")
        api_eps = recorder.api_endpoints()
        if api_eps:
            parts.append(f"Discovered {len(api_eps)} API endpoint(s).")
        if recorder.graphql_operations():
            parts.append("GraphQL activity observed: " + ", ".join(recorder.graphql_operations()) + ".")
        if recorder.websocket_endpoints():
            parts.append(f"WebSocket endpoints: {len(recorder.websocket_endpoints())}.")
        if state.challenges:
            parts.append(f"Challenges detected: {len(state.challenges)}.")
        if state.console_errors:
            parts.append(f"Console errors: {len(state.console_errors)}.")
        if not parts:
            parts.append("No significant findings.")
        return " ".join(parts)
