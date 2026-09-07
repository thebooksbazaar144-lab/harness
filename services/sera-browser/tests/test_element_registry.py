"""ElementRegistry unit + live tests (rebuild v4, Phase 4).

Pins the single-authoritative-namespace invariants:
- refs are registry-shaped (e001...), assigned at observation time
- identity is stable across snapshots (content signature matching)
- vanished elements tombstone (their refs stop resolving)
- invalidated refs NEVER come back, even if the element re-appears
- locator specs are data, never ElementHandles
- form fields / links match live registry refs (no fabrication)
"""
from __future__ import annotations

import pytest

from sera_browser.browser.element_registry import (
    ElementRegistry,
    content_signature,
    locator_strategies,
)
from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.inspector import Inspector
from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import InteractiveElement


pytestmark = pytest.mark.asyncio


def _el(role: str, name: str, **kw) -> InteractiveElement:
    return InteractiveElement(ref=None, role=role, name=name, **kw)


# ---------------------------------------------------------------------------
# Pure registry logic
# ---------------------------------------------------------------------------


def test_refs_are_registry_shaped_and_stable():
    r = ElementRegistry()
    els = [_el("link", "Home", tag="a", href="/"), _el("button", "Submit", tag="button")]
    snap = r.observe(els)
    assert all(e.ref and e.ref.startswith("e") and e.ref[1:].isdigit() for e in snap.elements)
    refs = [e.ref for e in snap.elements]
    snap2 = r.observe(els)
    assert [e.ref for e in snap2.elements] == refs, "identity must be stable"
    assert snap2.changed is False


def test_new_element_gets_new_ref_disappearing_tombstones():
    r = ElementRegistry()
    a, b, c = _el("link", "A"), _el("link", "B"), _el("link", "C")
    r.observe([a, b, c])
    assert {a.ref, b.ref, c.ref} == {"e001", "e002", "e003"}
    r.observe([a, b])  # c vanishes
    assert r.get(c.ref) is None, "vanished refs must stop resolving"
    r.observe([a, b, c])  # c returns with a NEW ref
    assert c.ref != "e003"
    assert r.get("e003") is None, "old ref stays dead"


def test_invalidation_is_permanent():
    r = ElementRegistry()
    a = _el("button", "Dead")
    r.observe([a])
    ref = a.ref
    r.invalidate(ref, "test")
    assert r.get(ref) is None
    # The element keeps appearing in snapshots — never re-minted.
    for _ in range(3):
        fresh = _el("button", "Dead")
        snap = r.observe([fresh])
        assert all(e.ref != ref for e in snap.elements)
    assert r.stats()["invalidated_total"] == 1


def test_failure_counting_invalidates():
    r = ElementRegistry()
    a = _el("button", "Flaky")
    r.observe([a])
    r.mark_failed(a.ref, "timeout")
    r.mark_failed(a.ref, "timeout")
    assert r.get(a.ref) is not None, "first two failures are tolerated"
    r.mark_failed(a.ref, "timeout")
    assert r.get(a.ref) is None, "third failure invalidates"


def test_locator_strategies_prefer_role_then_href():
    link = _el("link", "Docs", tag="a", href="/docs")
    specs = locator_strategies(link)
    assert specs[0]["type"] == "role" and specs[0]["role"] == "link"
    assert any(s["type"] == "css_href" and s["href"] == "/docs" for s in specs)
    tb = _el("textbox", "Email", tag="input", type="email", placeholder="you@x.com")
    specs_tb = locator_strategies(tb)
    assert specs_tb[0]["type"] == "role"
    assert any(s["type"] == "placeholder" for s in specs_tb)


def test_serialized_view_uses_registry_refs():
    r = ElementRegistry()
    r.observe([_el("button", "Go")])
    text = r.serialized()
    assert "[e001] button \"Go\"" in text


def test_content_signature_ignores_ref():
    a = _el("button", "Go")
    a.ref = "e001"
    b = _el("button", "Go")
    b.ref = "e999"
    assert content_signature(a) == content_signature(b)


# ---------------------------------------------------------------------------
# Live browser: driver + inspector integration
# ---------------------------------------------------------------------------


async def test_live_refs_and_form_matching(browser_manager, settings, local_site_url):
    """The full live path: driver snapshot → registry assignment → form
    fields matched to real refs; NO fabricated references anywhere."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="reg"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="reg"))
    registry = ElementRegistry()
    inspector = Inspector(driver=driver, registry=registry)

    summary = await inspector.observe(page)  # about:blank — fine
    await driver.navigate(page, local_site_url + "/login")
    summary = await inspector.observe(page)

    # 1. All element refs are registry-shaped.
    assert summary.elements, "login page must expose interactive elements"
    for e in summary.elements:
        assert e.ref and e.ref[0] == "e" and e.ref[1:].isdigit(), (
            f"non-registry ref leaked into the live path: {e.ref!r}"
        )

    # 2. The login form's fields are matched to LIVE registry refs (or None),
    #    never fabricated strings.
    assert summary.forms, "login page must expose the login form"
    form = summary.forms[0]
    assert form.has_password
    live_refs = {e.ref for e in summary.elements}
    for f in form.fields:
        assert f.ref is None or f.ref in live_refs, (
            f"fabricated field ref detected: {f.ref!r}"
        )
        assert not (f.ref and f.ref.startswith("form")), (
            f"namespace pollution: {f.ref!r}"
        )

    # 3. submit_ref is a REAL live ref (or None), never 'button_submit_N'.
    assert form.submit_ref is None or form.submit_ref in live_refs
    assert form.submit_ref != "button_submit_1"

    # 4. Links reference live anchor refs (or None), never 'linkN'.
    for l in summary.links:
        assert l.ref is None or l.ref in live_refs
        assert not (l.ref and l.ref.startswith("link") and l.ref[4:].isdigit())


async def test_live_resolution_returns_fresh_locators(browser_manager, settings, local_site_url):
    """Registry resolution returns locators that actually resolve on the
    live page (strategy fallback works)."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="reg"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="reg"))
    registry = ElementRegistry()
    inspector = Inspector(driver=driver, registry=registry)

    await driver.navigate(page, local_site_url + "/")
    summary = await inspector.observe(page)
    assert summary.elements
    some_ref = summary.elements[0].ref
    loc = await registry.resolve(page, some_ref)
    assert loc is not None, "live ref must resolve via locator strategies"
    # Unknown ref never resolves.
    assert await registry.resolve(page, "e000") is None


async def test_stuck_page_ref_still_resolves(browser_manager, settings, local_site_url):
    """An overlayed button is still VISIBLE: the registry resolves it —
    the click failure is the executor's bounded-budget job, not the
    registry's (this separation is what kills the 30s click)."""
    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="reg"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="reg"))
    registry = ElementRegistry()
    inspector = Inspector(driver=driver, registry=registry)

    await driver.navigate(page, local_site_url + "/stuck")
    summary = await inspector.observe(page)
    stuck = next(e for e in summary.elements if e.name == "Stuck Button")
    loc = await registry.resolve(page, stuck.ref)
    assert loc is not None, "visible-but-overlayed element must still resolve"
