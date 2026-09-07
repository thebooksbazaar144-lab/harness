"""OWASP Juice Shop tests — skipped when Juice Shop is not running locally.

Run with:
    juice-shop  # on port 3000
    pytest -m juice_shop
"""
from __future__ import annotations

import asyncio

import pytest

from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import TaskCreate
from sera_browser.session.manager import SessionManager


pytestmark = [pytest.mark.asyncio, pytest.mark.juice_shop]


def _juice_shop_url() -> str | None:
    """Probe for a local Juice Shop on the default port (3000).

    Retries for up to ~30s because Juice Shop's HTTP server may accept
    connections before the Angular frontend is fully ready, and the test
    fixture may be invoked before `npm start` has finished booting.
    """
    import time
    import urllib.error
    import urllib.request

    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:3000/", timeout=3.0).read(100)
            return "http://127.0.0.1:3000"
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return None


@pytest.fixture
def juice_url() -> str:
    url = _juice_shop_url()
    if url is None:
        pytest.skip("OWASP Juice Shop not running on :3000")
    return url


async def test_juice_shop_navigation(browser_manager, settings, juice_url):
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="js"))
    page = await cm.open(handle.browser)
    try:
        driver = PlaywrightDriver(settings, CancellationManager(task_id="js"))
        timing = await driver.navigate(page, juice_url + "/")
        assert timing.total_ms is not None
        # Juice Shop is an Angular SPA — title contains "OWASP Juice Shop"
        title = await page.title()
        assert "Juice" in title or "OWASP" in title, f"unexpected title: {title}"
    finally:
        await cm.close()


@pytest.fixture
def juice_settings(tmp_path, juice_url) -> Settings:
    """Settings tuned for Juice Shop (heavy Angular SPA)."""
    return Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost,127.0.0.1:3000",
        session_root=tmp_path / "sessions",
        screenshot_root=tmp_path / "screenshots",
        max_steps=20,
        log_level="INFO",
        log_format="text",
        network_idle_timeout_ms=5000,
        navigation_timeout_ms=60000,
    )


async def test_juice_shop_full_workflow(browser_manager, juice_settings, juice_url):
    """End-to-end autonomous agent task against Juice Shop."""
    sm = SessionManager(settings=juice_settings, browser_manager=browser_manager)
    req = TaskCreate(
        target=juice_url + "/",
        task=(
            "Analyze this application for business flows, authentication, "
            "APIs, routes, technologies and security-relevant browser "
            "behavior."
        ),
        browser_mode="headless",
        timeout=180,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost", "127.0.0.1:3000"],
        max_steps=20,
    )
    rec = await sm.create(req)
    while rec.task is not None and not rec.task.done():
        await asyncio.sleep(0.5)
    assert rec.report is not None, f"no report; status={rec.status}, error={rec.error}"
    report = rec.report
    # Juice Shop is Angular
    assert any("Angular" in t for t in report.technology), f"tech: {report.technology}"
    # Should have discovered routes (SPA)
    assert len(report.routes) >= 1, f"routes: {report.routes}"
    # Should have discovered APIs (/rest/* and /api/*)
    apis = report.apis
    assert any("/rest/" in a for a in apis), f"apis: {apis}"
    # Network should have many requests
    assert report.network.request_count > 5, f"net: {report.network}"
    # Authentication summary should note the login form OR at least one form
    # with a password field. Juice Shop may render the login form lazily;
    # we accept either an auth detection OR a route that points to /login.
    auth_ok = (
        report.authentication.detected
        or any("/login" in r for r in report.routes)
        or any("/login" in a for a in apis)
    )
    assert auth_ok, f"no auth indicator: routes={report.routes}, apis={apis}"


# ---------------------------------------------------------------------------
# v4 ACCEPTANCE (rebuild browser-intelligence-v4, Phase 17)
#
# The acceptance bar is AUTONOMOUS EXPLORATION, not browser startup:
#   1. Coverage-driven: real routes visited, not just "the app loaded".
#   2. No 30-second repeated clicks: every action bounded (evidence-driven).
#   3. No infinite loops: the task terminates inside its budget with a
#      coverage-based summary.
#   4. No unbounded vision: zero or ≤2 vision calls, no screenshot storm.
#   5. Registry refs everywhere; no fabricated references in the report.
#   6. Machine-readable run package written to the session directory.
#   7. Workflows are observed correlations, not heuristic labels.
# ---------------------------------------------------------------------------


async def test_juice_shop_v4_acceptance(browser_manager, juice_settings, juice_url, tmp_path):
    """Phase 17 acceptance: the agent explores Juice Shop autonomously and
    provably without the v3 pathologies (loop→screenshot→loop, ~30s stuck
    clicks, fabricated refs, fake auth, count-only extraction)."""
    from sera_browser.task.agent import BrowserAgent, TaskConfig

    settings = juice_settings.model_copy(
        update={
            "max_steps": 25,
            "action_timeout_ms": 6000,
            "action_retry_timeout_ms": 2000,
            "log_level": "WARNING",
        }
    )
    agent = BrowserAgent(settings=settings, browser_manager=browser_manager)
    cfg = TaskConfig(
        task_id="juice-v4-acceptance",
        target=juice_url + "/",
        objective=(
            "Autonomously explore this application: map its routes, forms, "
            "API endpoints, workflows and authentication surface."
        ),
        browser_mode="headless",
        timeout_s=150,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost", "127.0.0.1:3000"],
        max_steps=25,
    )
    cancellation = CancellationManager(task_id=cfg.task_id)
    result = await agent.run(cfg, cancellation)
    assert result.error is None, f"task error: {result.error}"
    report = result.report
    assert report is not None

    # --- 3. Terminated cleanly, inside the budget -----------------------
    assert report.status in {"completed", "blocked", "timeout"}, report.status
    assert report.duration_ms is not None and report.duration_ms < 150_000
    assert report.steps <= 30

    # --- 1. Coverage-driven autonomous exploration ----------------------
    assert report.coverage is not None
    visited = report.coverage["routes"]["visited"]
    discovered = report.coverage["routes"]["discovered"]
    assert discovered >= 3, f"SPA must yield routes: {report.coverage['routes']}"
    assert visited >= 2, (
        f"autonomous exploration must VISIT routes, not just discover: {report.coverage['routes']}"
    )
    # REST surface observed via exploration (not mere startup traffic).
    assert report.apis and any("/rest/" in a for a in report.apis)

    # --- 2. No 30-second repeated clicks (evidence-driven) --------------
    assert report.evidence, "evidence ledger must be populated"
    slow_actions = [
        e for e in report.evidence if e.get("duration_ms", 0) > 15_000
    ]
    assert not slow_actions, (
        f"actions exceeding the bounded budget: "
        f"{[(e['action'], e['duration_ms']) for e in slow_actions]}"
    )
    # Repeated identical failing actions must not appear > 2 times.
    from collections import Counter

    fail_keys = Counter(
        (e["action"], e.get("ref")) for e in report.evidence if not e.get("ok")
    )
    assert all(c <= 3 for c in fail_keys.values()), (
        f"same action repeated past the block limit: {fail_keys}"
    )

    # --- 4. No screenshot storm / vision budget -------------------------
    assert len(report.screenshots) <= 6, (
        f"screenshot storm suspected: {len(report.screenshots)}"
    )

    # --- 5. Registry namespace (no fabricated refs) ---------------------
    import re

    e_ref = re.compile(r"^e\d{3,}$")
    for f in report.forms:
        for field in f.get("fields", []):
            ref = field.get("ref")
            assert ref is None or e_ref.match(ref), f"fabricated ref: {ref!r}"

    # --- 6. Machine-readable run package --------------------------------
    import json as _json
    from pathlib import Path

    pkg_path = Path(settings.session_root) / cfg.task_id / "run_package.json"
    assert pkg_path.exists(), "run_package.json must be written"
    pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
    assert pkg["schema"] == "sera-browser.run-package/v1"
    assert pkg["application"]["routes"], "package must carry the route map"
    assert pkg["endpoints"], "package must carry discovered endpoints"
    assert "auth_states" in pkg and "coverage" in pkg

    # --- 7. Real workflow correlations ----------------------------------
    assert report.app_graph is not None
    assert report.app_graph["nodes"]["routes"], "app graph must have route nodes"

    # --- Auth: no credentials provided ⇒ no AUTHENTICATED claim ---------
    assert report.auth_states is not None
    assert report.auth_states["state"] != "AUTHENTICATED"
