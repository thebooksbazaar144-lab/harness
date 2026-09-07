"""State maintenance helpers.

`BrowserState` lives in `models.py` (Pydantic model). This module provides
the small set of helpers that update state from observation snapshots and
from network/challenge events. Keeping these helpers here prevents the rest
of the agent from mutating `BrowserState` ad-hoc.
"""
from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..browser.inspector import SnapshotSummary
from ..logging import get_logger
from ..models import BrowserState

log = get_logger("sera.state")


def update_from_snapshot(state: BrowserState, summary: SnapshotSummary) -> BrowserState:
    state.current_url = summary.url
    state.title = summary.title
    state.snapshot = summary.tree
    state.interactive_elements = summary.elements
    # Merge forms (dedup by action+method) and links (dedup by href) — the
    # report is an ANALYSIS artifact: forms/links discovered on earlier pages
    # must survive navigation to later pages. (Routes were already merged.)
    state.forms = _merge_forms(state.forms, summary.forms)
    state.links = _merge_links(state.links, summary.links)
    # Merge routes (don't lose previously discovered ones).
    existing = set(state.routes)
    for r in summary.routes:
        existing.add(r)
    state.routes = sorted(existing)
    state.recompute_dom_hash(summary.raw)
    state.timings.snapshot_ms = summary.snapshot_ms
    # page_type heuristic
    state.page_type = _classify_page(state)
    return state


def _merge_forms(existing: Any, new: Any) -> Any:
    """Merge form descriptors across observations, dedup by action+method."""
    merged: dict[str, Any] = {}
    for f in list(existing or []) + list(new or []):
        key = f"{f.action}|{f.method}"
        if key not in merged:
            merged[key] = f
    return list(merged.values())


def _merge_links(existing: Any, new: Any) -> Any:
    """Merge link descriptors across observations, dedup by href."""
    merged: dict[str, Any] = {}
    for l in list(existing or []) + list(new or []):
        key = l.href
        if key not in merged:
            merged[key] = l
    return list(merged.values())


def _classify_page(state: BrowserState) -> str:
    if any(c.type == "login_challenge" for c in state.challenges):
        return "login"
    if any(c.type in {"cloudflare_interstitial", "bot_detection", "verify_human"} for c in state.challenges):
        return "challenge"
    if state.current_url and any(
        s in state.current_url.lower() for s in ("login", "signin", "auth")
    ):
        return "login"
    if any(t in {"Next.js", "Nuxt", "React", "Vue", "Angular", "AngularJS"} for t in state.technologies):
        return "spa"
    if state.console_errors and len(state.page_errors) > 3:
        return "error"
    return "static"


def merge_network(state: BrowserState, recorder) -> BrowserState:  # type: ignore[no-untyped-def]
    state.requests = recorder.requests
    state.responses = recorder.responses
    state.websockets = recorder.websockets
    return state


def merge_storage(state: BrowserState, cookies, ls_keys, ss_keys) -> BrowserState:  # type: ignore[no-untyped-def]
    state.cookies = cookies
    state.local_storage_keys = ls_keys
    state.session_storage_keys = ss_keys
    return state


def merge_challenges(state: BrowserState, challenges: list) -> BrowserState:  # type: ignore[no-untyped-def]
    existing = {(c.type, c.evidence) for c in state.challenges}
    for c in challenges:
        if (c.type, c.evidence) not in existing:
            state.challenges.append(c)
            existing.add((c.type, c.evidence))
    # Re-classify page_type after challenge merge
    state.page_type = _classify_page(state)
    return state


def mark_authenticated(state: BrowserState, role: str | None = None) -> BrowserState:
    state.authenticated = True
    if role:
        state.user_role = role
    state.page_type = _classify_page(state)
    return state
