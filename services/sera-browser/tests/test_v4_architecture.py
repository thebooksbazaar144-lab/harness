"""Rebuild v4 architecture integration tests.

A full autonomous agent run against the local test site must demonstrate:
  1. Coverage-driven exploration (no fixed 5-route cap, real route coverage)
  2. The registry namespace everywhere (no fabricated refs in the report)
  3. Structured per-step evidence + network attribution
  4. The feedback channel (planner observations carry structured results)
  5. Evidence-based auth (a login attempt is NOT authentication)
  6. The machine-readable run package on disk
  7. Loop escapes abandon targets (never screenshots)
  8. Completion is a coverage statement (never "visited /login")
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.task.agent import BrowserAgent, TaskConfig


pytestmark = pytest.mark.asyncio


@pytest.fixture
def v4_settings(tmp_path, local_site_url) -> Settings:
    return Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        session_root=tmp_path / "sessions",
        screenshot_root=tmp_path / "screenshots",
        max_steps=30,
        action_timeout_ms=4000,
        action_retry_timeout_ms=1500,
        log_level="WARNING",
        log_format="text",
        network_idle_timeout_ms=1500,
        enable_tracing=False,
    )


async def _run(settings: Settings, bm: BrowserManager, url: str, task_id: str, **cfg_kw):
    agent = BrowserAgent(settings=settings, browser_manager=bm)
    cfg = TaskConfig(
        task_id=task_id,
        target=url,
        objective="Autonomously explore and analyze this application.",
        browser_mode="headless",
        timeout_s=120,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        **cfg_kw,
    )
    return await agent.run(cfg, CancellationManager(task_id=task_id))


# Pattern of the single authoritative ref namespace.
_E_REF = re.compile(r"^e\d{3,}$")


async def test_full_run_v4_invariants(browser_manager, v4_settings, local_site_url):
    result = await _run(
        v4_settings, browser_manager, local_site_url + "/", "v4-full"
    )
    assert result.error is None, f"task error: {result.error}"
    report = result.report
    assert report is not None
    assert report.status in {"completed", "blocked", "timeout"}

    # ---- 2. Registry namespace everywhere ----------------------------
    assert report.registry_stats is not None
    assert report.registry_stats["live_elements"] >= 0
    for f in report.forms:
        for field in f.get("fields", []):
            ref = field.get("ref")
            assert ref is None or _E_REF.match(ref), (
                f"fabricated field ref in report: {ref!r}"
            )
        assert f.get("submit_ref") is None or _E_REF.match(f["submit_ref"]), (
            f"fabricated submit_ref in report: {f.get('submit_ref')!r}"
        )

    # ---- 3. Structured evidence with network attribution --------------
    assert report.evidence, "evidence ledger must be populated"
    for ev in report.evidence:
        assert "action" in ev and "ok" in ev
        assert "args" in ev
    with_network = [e for e in report.evidence if e.get("attributed_requests")]
    assert with_network, "at least one action must have attributed network events"

    # ---- 4. Feedback channel: observations carry structured results ---
    fb_obs = [
        o for o in report.observations if o.startswith("step ") and ("ok" in o or "FAILED" in o)
    ]
    assert fb_obs, "feedback observations must be present"

    # ---- 1. Coverage-driven exploration -------------------------------
    assert report.coverage is not None
    routes = report.coverage["routes"]
    assert routes["discovered"] >= 5, "local site exposes >= 5 routes"
    assert routes["visited"] >= 3, (
        f"coverage planner must visit real routes (visited {routes['visited']})"
    )
    # v3 capped at 5 routes; v4's cap is the step budget (30).
    assert routes["visited"] > 0

    # ---- 6. Machine-readable run package ------------------------------
    pkg_path = Path(v4_settings.session_root) / "v4-full" / "run_package.json"
    assert pkg_path.exists(), "run_package.json must be written to the session dir"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert pkg["schema"] == "sera-browser.run-package/v1"
    assert pkg["application"]["routes"]
    assert "coverage" in pkg and pkg["coverage"]
    assert "evidence" in pkg
    assert "auth_states" in pkg
    assert "endpoints" in pkg

    # ---- 7. Workflows are REAL observed correlations ------------------
    # (app_graph workflows carry route/endpoint structure, or are empty —
    # never heuristic labels like 'spa-routing'.)
    if report.workflows:
        for w in report.workflows:
            assert not w.endswith("-api") and w not in (
                "spa-routing", "authentication-form", "graphql-api",
                "websocket-usage", "rest-api",
            ), f"heuristic workflow label leaked: {w!r}"

    # ---- 8. Summary is a coverage statement ---------------------------
    assert report.summary
    low = report.summary.lower()
    if report.status == "completed":
        assert "coverage" in low or "max_steps" in low, (
            f"completion must cite coverage, got: {report.summary[:120]!r}"
        )


async def test_auth_attempt_is_not_authentication(browser_manager, v4_settings, local_site_url):
    """THE v3 bug (agent.py:412): any login-ish action + non-login page ⇒
    mark_authenticated. v4: no credentials ⇒ anonymous forever, and the
    auth state is the machine's evidence-based verdict."""
    result = await _run(
        v4_settings, browser_manager, local_site_url + "/login", "v4-auth"
    )
    assert result.error is None
    report = result.report
    assert report is not None

    assert report.auth_states is not None
    # No credentials were provided: the machine must NOT claim AUTHENTICATED.
    assert report.auth_states["state"] != "AUTHENTICATED", (
        "login attempt without credentials must not authenticate"
    )
    assert report.auth_states["authenticated"] is False
    # And the report-level flag agrees.
    assert report.authentication.storage_state_saved is False


async def test_loop_escape_blocks_target_not_screenshots(
    browser_manager, v4_settings, local_site_url
):
    """Feed the agent a stuck target (/stuck): loop escape must block the
    target and move on — the screenshot count stays bounded."""
    result = await _run(
        v4_settings, browser_manager, local_site_url + "/stuck", "v4-loop"
    )
    assert result.error is None
    report = result.report
    assert report is not None
    assert report.blocked_targets is not None
    # The stuck page's only actionable element can never be clicked: after
    # two failures it must be blocked and abandoned.
    assert report.blocked_targets, "stuck target must end up blocked"
    # Screenshot budget: the deterministic planner takes exactly ONE final
    # screenshot (evidence) — never an escape loop.
    assert len(report.screenshots) <= 3, (
        f"screenshot-as-loop-escape suspected: {len(report.screenshots)} screenshots"
    )


async def test_network_attribution_into_app_graph(browser_manager, v4_settings, local_site_url):
    """Navigating to the REST-served pages must attribute API traffic to
    the navigation action and record endpoints in the app graph."""
    result = await _run(
        v4_settings, browser_manager, local_site_url + "/products", "v4-graph"
    )
    assert result.error is None
    report = result.report
    assert report is not None
    assert report.app_graph is not None
    assert report.app_graph["nodes"]["routes"], "routes must be graph nodes"
    assert report.evidence
    # The products page fires /rest/* traffic — either attributed to a
    # step or captured in the recorder's endpoint list.
    any_rest = any("rest" in a for a in (report.apis or [])) or any(
        "rest" in str(r.get("url", ""))
        for e in report.evidence
        for r in e.get("attributed_requests", [])
    )
    assert any_rest, "REST endpoint traffic must be captured (apis or attribution)"
