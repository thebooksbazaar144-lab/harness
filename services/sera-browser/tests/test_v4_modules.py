"""Unit tests for the v4 subsystem modules: coverage engine, auth state
machine, app graph, evidence log, and the deterministic coverage planner.
"""
from __future__ import annotations

import pytest

from sera_browser.config import Settings
from sera_browser.models import (
    BrowserState,
    FormDescriptor,
    InteractiveElement,
    LinkDescriptor,
    NetworkRequest,
)
from sera_browser.task.app_graph import ApplicationGraph
from sera_browser.task.auth_state import AuthStateMachine
from sera_browser.task.coverage import CoverageEngine
from sera_browser.task.evidence import EvidenceLog
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.planner import StepFeedback, TaskPlanner


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# CoverageEngine
# ---------------------------------------------------------------------------


def _state(**kw) -> BrowserState:
    s = BrowserState(task_id="t", target="http://127.0.0.1:9999/")
    s.current_url = "http://127.0.0.1:9999/"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_coverage_routes_flow():
    cov = CoverageEngine()
    state = _state(routes=["/a", "/b", "/c"])
    cov.ingest_state(state)
    assert cov.unexplored_routes(state) == ["/a", "/b", "/c"]
    cov.record_visit("http://127.0.0.1:9999/a")
    assert cov.unexplored_routes(state) == ["/b", "/c"]
    target = cov.next_target(state)
    assert target is not None and target.kind == "route" and target.value == "/b"


def test_coverage_blocked_targets_are_skipped():
    cov = CoverageEngine()
    loop = LoopRecoveryManager()
    cov.attach_loop(loop)
    state = _state(routes=["/a", "/b"])
    cov.ingest_state(state)
    loop.block_route("/a")
    cov.sync_blocked(loop)
    assert cov.unexplored_routes(state) == ["/b"]
    target = cov.next_target(state)
    assert target is not None and target.value == "/b"


def test_coverage_elements_priority_and_completion():
    cov = CoverageEngine()
    els = [
        InteractiveElement(ref="e001", role="button", name="B1"),
        InteractiveElement(ref="e002", role="link", name="L1"),
        InteractiveElement(ref="e003", role="textbox", name="T1"),
    ]
    state = _state(interactive_elements=els)
    cov.ingest_state(state)
    unexplored = cov.unexplored_elements(state)
    # Links first.
    assert unexplored[0]["ref"] == "e002"
    cov.record_interaction("e002")
    cov.record_interaction("e001")
    cov.record_interaction("e003")
    assert cov.unexplored_elements(state) == []
    # With no routes/forms left, completion fires.
    state2 = _state(interactive_elements=els, routes=[], forms=[])
    cov.discovered_routes.clear()
    cov.ingest_state(state2)
    reason = cov.completion_reason(state2)
    assert reason is not None and "coverage complete" in reason


def test_coverage_summary_is_machine_readable():
    cov = CoverageEngine()
    state = _state(routes=["/a"])
    cov.ingest_state(state)
    # In-scope visit counts; off-origin (redirect) does not.
    cov.record_visit("http://127.0.0.1:9999/a")
    cov.record_visit("https://github.com/some/repo")
    cov.ingest_network(["GET /api/x"])
    cov.record_auth_state("ANONYMOUS")
    summary = cov.summary()
    assert summary["routes"]["discovered"] == 1
    assert summary["routes"]["visited"] == 1
    assert summary["routes"]["percent"] == 100.0
    assert summary["endpoints"]["discovered"] == 1
    assert summary["auth_states"]["reached"] == ["ANONYMOUS"]
    # The external domain is OBSERVED (open-redirect evidence), not covered.
    assert summary["external_domains_observed"] == ["https://github.com"]


def test_normalize_route_collapses_data_routes():
    from sera_browser.task.coverage import normalize_route

    assert normalize_route("/product/1") == "/product/:id"
    assert normalize_route("/product/42") == "/product/:id"
    assert normalize_route("/login") == "/login"
    assert normalize_route("/user/3f2b8c1a-9d4e-4f2a-b1c3-112233445566") == "/user/:uuid"
    assert normalize_route("/asset/deadbeef0011") == "/asset/:hash"


def test_coverage_scopes_to_target_origin():
    cov = CoverageEngine()
    cov.ingest_state(_state(routes=["/a"]))  # locks scope to 127.0.0.1:9999
    # Off-origin observation: nothing ingested (github redirect scenario).
    off = _state(routes=["/Aashish683", "/CS-F301"])
    off.current_url = "https://github.com/Aashish683/CS-F301"
    cov.ingest_state(off)
    assert cov.discovered_routes == {"/a"}, "off-origin routes must not pollute coverage"


# ---------------------------------------------------------------------------
# AuthStateMachine — attempt is NOT authentication
# ---------------------------------------------------------------------------


def test_login_attempt_alone_never_authenticates():
    m = AuthStateMachine()
    state = _state(
        forms=[FormDescriptor(ref="form1", has_password=True, submit_ref="e001")],
    )
    m.evaluate(state, login_just_attempted=True)
    assert m.state in ("LOGIN_ATTEMPT", "LOGIN_FAILED"), m.state
    assert m.state != "AUTHENTICATED"
    assert state.authenticated is False


def test_multiple_postconditions_authenticate():
    m = AuthStateMachine()
    state = _state(
        forms=[],
        cookies=[{"name": "connect.sid", "value": "abc"}],
        local_storage_keys=["token"],
        interactive_elements=[InteractiveElement(ref="e001", role="link", name="Logout")],
    )
    m.evaluate(state, login_just_attempted=True)
    assert m.state == "AUTHENTICATED", m.state
    assert state.authenticated is True


def test_single_weak_signal_is_not_enough():
    m = AuthStateMachine()
    # Only a session cookie — pre-auth CSRF/session cookies are common.
    state = _state(cookies=[{"name": "session", "value": "x"}])
    m.evaluate(state, login_just_attempted=True)
    assert m.state != "AUTHENTICATED"


def test_auth_error_blocks_transition():
    m = AuthStateMachine()
    req = NetworkRequest(id="r1", method="GET", url="http://x/api/me", status=401, resource_type="xhr")
    state = _state(
        forms=[],
        cookies=[{"name": "sid", "value": "y"}],
        local_storage_keys=["token"],
    )
    state.requests.append(req)
    m.evaluate(state, login_just_attempted=True)
    assert m.state == "LOGIN_FAILED", m.state


def test_auth_summary_has_history():
    m = AuthStateMachine()
    m.evaluate(_state(), login_just_attempted=False)
    s = m.summary()
    assert s["state"] == "ANONYMOUS"
    assert s["authenticated"] is False
    assert "states_reached" in s


# ---------------------------------------------------------------------------
# ApplicationGraph
# ---------------------------------------------------------------------------


def test_app_graph_correlates_action_to_endpoint_and_route():
    g = ApplicationGraph()
    edge = g.record_action(
        step=3,
        action="click",
        ref="e007",
        url_before="http://x/search",
        url_after="http://x/products",
        attributed_requests=[
            {"method": "GET", "url": "http://x/rest/items?q=apple", "status": 200, "resource_type": "xhr"},
            {"method": "GET", "url": "http://x/static/logo.png", "status": 200, "resource_type": "image"},
        ],
        ok=True,
    )
    assert edge is not None
    assert edge.route_changed is True
    assert edge.endpoints == ["GET /rest/items"], "static assets excluded, query stripped"
    wfs = g.workflows()
    assert wfs and wfs[0]["workflow"] == "click /search → /products"
    d = g.to_dict()
    assert "/products" in d["nodes"]["routes"]
    assert "GET /rest/items" in d["nodes"]["endpoints"]


def test_app_graph_noop_actions_not_recorded():
    g = ApplicationGraph()
    assert g.record_action(1, "noop", None, "http://x", "http://x", []) is None
    assert g.record_action(2, "screenshot", None, "http://x", "http://x", []) is None


# ---------------------------------------------------------------------------
# EvidenceLog
# ---------------------------------------------------------------------------


def test_evidence_redacts_sensitive_args(tmp_path):
    log = EvidenceLog(tmp_path)
    log.record(
        step=1,
        action="fill",
        args={"ref": "e001", "value": "super-secret-password"},
        ok=True,
        failure=None,
        detail="ok",
        ref="e001",
        url_before="http://x/login",
        url_after="http://x/login",
        attributed_requests=[],
    )
    entry = log.to_list()[0]
    assert entry["args"]["value"].startswith("<redacted")
    assert (tmp_path / "evidence.jsonl").exists()


def test_evidence_summary_counts():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        log = EvidenceLog(Path(td))
        log.record(step=1, action="click", args={}, ok=True, failure=None, detail="", ref="e1",
                   url_before="http://x/a", url_after="http://x/b", attributed_requests=[{"m": 1}])
        log.record(step=2, action="click", args={}, ok=False, failure={"kind": "timeout"}, detail="", ref="e1",
                   url_before="http://x/b", url_after="http://x/b", attributed_requests=[])
        s = log.summary()
        assert s["steps"] == 2
        assert s["failed_steps"] == 1
        assert s["route_changes"] == 1
        assert s["failure_kinds"] == ["timeout"]


# ---------------------------------------------------------------------------
# Deterministic coverage planner
# ---------------------------------------------------------------------------


async def test_planner_uses_coverage_compass():
    settings = Settings(local_lab=True)
    cov = CoverageEngine()
    planner = TaskPlanner(settings, coverage=cov)
    loop = LoopRecoveryManager()
    state = _state(routes=["/a", "/b"])
    state.dom_hash = "x" * 40
    state.technologies = ["jQuery"]
    cov.ingest_state(state)

    # First real step after observe: explore route /a.
    step = await planner.next_step(state, loop)
    assert step.kind == "explore_routes" and step.args["url"].endswith("/a")

    # Simulate visiting /a and re-observing.
    state.current_url = "http://127.0.0.1:9999/a"
    state.routes = ["/a", "/b", "/c"]
    cov.ingest_state(state)
    planner.receive_feedback(
        StepFeedback(step=2, action="navigate", ok=True, url_after="http://127.0.0.1:9999/a")
    )
    cov.record_visit("http://127.0.0.1:9999/a")
    step2 = await planner.next_step(state, loop)
    assert step2.kind == "explore_routes" and step2.args["url"].endswith("/b")


async def test_planner_finishes_with_coverage_statement():
    settings = Settings(local_lab=True)
    cov = CoverageEngine()
    planner = TaskPlanner(settings, coverage=cov)
    loop = LoopRecoveryManager()
    state = _state(routes=[], forms=[], interactive_elements=[], screenshots=["x.png"])
    state.dom_hash = "x" * 40
    state.technologies = ["jQuery"]
    cov.ingest_state(state)
    step = await planner.next_step(state, loop)
    assert step.kind == "finish"
    assert "coverage" in step.reason or "frontier" in step.reason


async def test_planner_loop_escape_abandons_target():
    settings = Settings(local_lab=True)
    cov = CoverageEngine()
    planner = TaskPlanner(settings, coverage=cov)
    loop = LoopRecoveryManager(tool_repeat_limit=2)
    state = _state(
        routes=["/a", "/b"],
        interactive_elements=[InteractiveElement(ref="e001", role="button", name="B")],
    )
    state.dom_hash = "x" * 40
    state.technologies = ["jQuery"]
    cov.ingest_state(state)

    # The planner is stuck clicking e001 (loop fires).
    loop.record_tool("click", "e001")
    loop.record_tool("click", "e001")
    step = await planner.next_step(state, loop)
    # Loop escape ran: e001 blocked; the planner must pick a route instead.
    assert "e001" in loop.blocked_refs
    assert step.kind == "explore_routes", f"expected route exploration, got {step.kind}"
    assert step.kind != "capture_screenshot"
