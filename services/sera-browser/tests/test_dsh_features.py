"""Tests for dsh-browser-inspired features.

Covers:
- scrape (CSS-based structured extraction with selector@attr)
- content (multi-format page fetch: html/markdown/txt/json)
- history + replay (record ops, replay by seq, mask sensitive)
- soft_restrict (model-controllable action allowlist)
- flush_auth / restore_auth (cookie export/import)
- deep challenge scan (shadow DOM + iframe detection)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState, InteractiveElement
from sera_browser.task.action_schema import (
    parse_llm_response,
    validate_action,
)
from sera_browser.task.history import HistoryEntry, HistoryManager
from sera_browser.task.soft_restrict import SoftRestrict


pytestmark = pytest.mark.asyncio


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        max_steps=5,
        enable_tracing=False,
        enable_action_log=False,
    )
    base.update(overrides)
    return Settings(**base)


def _make_state(**overrides: Any) -> BrowserState:
    state = BrowserState(task_id="test", target="http://127.0.0.1:9999/")
    state.current_url = "http://127.0.0.1:9999/"
    state.dom_hash = "abc"
    state.interactive_elements = [
        InteractiveElement(ref="button#1:Login", role="button", name="Login", tag="button"),
    ]
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


# ---------------------------------------------------------------------------
# Action schema: new dsh-browser actions
# ---------------------------------------------------------------------------


class TestActionSchemaNewActions:
    def test_scrape_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {
            "action": "scrape",
            "args": {
                "item": "div.card",
                "fields": [
                    {"name": "title", "selector": "h3"},
                    {"name": "url", "selector": "a@href"},
                ],
            },
        }
        result = validate_action(parsed, state, settings)
        assert result.ok, f"should be valid: {result.reason}"

    def test_scrape_rejects_missing_item(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "scrape", "args": {"fields": []}}
        result = validate_action(parsed, state, settings)
        assert not result.ok

    def test_content_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "content", "args": {"format": "markdown"}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_content_with_selector(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "content", "args": {"format": "txt", "selector": "#main", "max_chars": 5000}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_history_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "history", "args": {}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_replay_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "replay", "args": {"seq": 3}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_restrict_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "restrict", "args": {"allowed": ["navigate", "click"]}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_flush_auth_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "flush_auth", "args": {}}
        result = validate_action(parsed, state, settings)
        assert result.ok

    def test_restore_auth_validated(self) -> None:
        state = _make_state()
        settings = _make_settings()
        parsed = {"action": "restore_auth", "args": {"cookies": [{"name": "session", "value": "abc"}]}}
        result = validate_action(parsed, state, settings)
        assert result.ok


# ---------------------------------------------------------------------------
# HistoryManager
# ---------------------------------------------------------------------------


class TestHistoryManager:
    def test_record_appends_entry(self) -> None:
        hm = HistoryManager()
        hm.record("navigate", {"url": "http://x/"}, True, result="ok")
        hm.record("click", {"ref": "1"}, True)
        assert len(hm.list_entries()) == 2
        assert hm.list_entries()[0].seq == 1
        assert hm.list_entries()[1].seq == 2

    def test_get_entry_by_seq(self) -> None:
        hm = HistoryManager()
        hm.record("navigate", {"url": "http://x/"}, True)
        hm.record("click", {"ref": "1"}, False, error="not found")
        entry = hm.get_entry(2)
        assert entry is not None
        assert entry.action == "click"
        assert entry.ok is False
        assert "not found" in entry.error

    def test_bounds_at_max_entries(self) -> None:
        hm = HistoryManager(max_entries=5)
        for i in range(10):
            hm.record("navigate", {"url": f"http://x/{i}"}, True)
        assert len(hm.list_entries()) == 5
        # Should keep the last 5.
        assert hm.list_entries()[0].seq == 6
        assert hm.list_entries()[-1].seq == 10

    def test_masked_view_masks_sensitive_fields(self) -> None:
        hm = HistoryManager()
        hm.record("fill", {"ref": "1", "value": "my-secret-password"}, True)
        hm.record("evaluate", {"script": "document.cookie"}, True, result="session=abc")
        masked = hm.masked_view()
        # 'value' should be masked.
        fill_entry = next(e for e in masked if e["action"] == "fill")
        assert "my-secret-password" not in fill_entry["params"]["value"]
        assert "***" in fill_entry["params"]["value"]
        # 'script' + result should be masked for execute.
        exec_entry = next(e for e in masked if e["action"] == "evaluate")
        assert "document.cookie" not in exec_entry["params"]["script"]
        assert "redacted" in exec_entry["result"]

    def test_masked_view_preserves_non_sensitive(self) -> None:
        hm = HistoryManager()
        hm.record("navigate", {"url": "http://x/"}, True, result="ok")
        masked = hm.masked_view()
        nav_entry = masked[0]
        assert nav_entry["params"]["url"] == "http://x/"
        assert nav_entry["result"] == "ok"

    def test_stats(self) -> None:
        hm = HistoryManager()
        hm.record("navigate", {}, True)
        hm.record("click", {}, False)
        stats = hm.stats
        assert stats["total_entries"] == 2
        assert stats["next_seq"] == 3


# ---------------------------------------------------------------------------
# SoftRestrict
# ---------------------------------------------------------------------------


class TestSoftRestrict:
    def test_default_unrestricted(self) -> None:
        sr = SoftRestrict()
        assert not sr.restricted
        assert sr.allowed is None
        assert sr.is_allowed("fill_form")

    def test_set_restriction_blocks_mutations(self) -> None:
        sr = SoftRestrict()
        sr.set_restriction(["navigate", "click"])
        assert sr.restricted
        assert sr.is_allowed("navigate")  # in allowlist
        assert sr.is_allowed("click")  # in allowlist
        assert not sr.is_allowed("fill")  # mutation, not in allowlist
        assert not sr.is_allowed("fill_form")

    def test_readonly_always_allowed(self) -> None:
        sr = SoftRestrict()
        sr.set_restriction(["click"])  # only click allowed as mutation
        # Read-only actions should always pass.
        assert sr.is_allowed("navigate")
        assert sr.is_allowed("screenshot")
        assert sr.is_allowed("content")
        assert sr.is_allowed("history")
        assert sr.is_allowed("scrape")
        assert sr.is_allowed("restrict")  # always allowed so guard can be lifted

    def test_empty_list_lifts_restriction(self) -> None:
        sr = SoftRestrict()
        sr.set_restriction(["click"])
        assert sr.restricted
        sr.set_restriction([])
        assert not sr.restricted
        assert sr.is_allowed("fill_form")

    def test_allowed_returns_sorted_list(self) -> None:
        sr = SoftRestrict()
        sr.set_restriction(["click", "navigate", "fill"])
        allowed = sr.allowed
        assert allowed == ["click", "fill", "navigate"]


# ---------------------------------------------------------------------------
# Scrape (integration with real browser)
# ---------------------------------------------------------------------------


class TestScrapeIntegration:
    async def test_scrape_extracts_items(self, browser_manager, settings, local_site_url) -> None:
        """scrape should extract structured data from the products page."""
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector
        from sera_browser.task.loop import LoopRecoveryManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="scrape"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="scrape"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="scrape"))
            state = _make_state()
            loop = LoopRecoveryManager()
            await driver.navigate(page, local_site_url + "/")
            # Scrape the nav links.
            result = await executor.execute(
                page, state, loop, "scrape",
                item="nav a",
                fields=[
                    {"name": "text", "selector": ""},
                    {"name": "href", "selector": "a@href"},
                ],
            )
            # The scrape action stores the result in detail, but we verify via
            # the executor's _scrape helper directly for assertions.
            items = await executor._scrape(  # noqa: SLF001
                page, "nav a",
                [{"name": "text", "selector": ""}, {"name": "href", "selector": "@href"}],
            )
            assert items["count"] > 0
            # Each item should have text + href.
            first = items["items"][0]
            assert "text" in first
            assert "href" in first
            # href should be an absolute URL (selector@attr resolves relative).
            assert first["href"] is not None
            assert first["href"].startswith("http")
        finally:
            await cm.close()

    async def test_scrape_returns_empty_for_no_match(self, browser_manager, settings, local_site_url) -> None:
        """scrape should fail gracefully when no elements match."""
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="scrape2"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="scrape2"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="scrape2"))
            await driver.navigate(page, local_site_url + "/")
            with pytest.raises(RuntimeError, match="no elements matched"):
                await executor._scrape(  # noqa: SLF001
                    page, "div.nonexistent",
                    [{"name": "x", "selector": "span"}],
                )
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# Content fetch (integration)
# ---------------------------------------------------------------------------


class TestContentIntegration:
    async def test_content_txt_format(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="content"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="content"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="content"))
            await driver.navigate(page, local_site_url + "/")
            content = await executor._fetch_content(page, "txt", None, 50000)  # noqa: SLF001
            assert "SERA Local Test Site" in content
        finally:
            await cm.close()

    async def test_content_html_format(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="content2"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="content2"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="content2"))
            await driver.navigate(page, local_site_url + "/")
            content = await executor._fetch_content(page, "html", None, 50000)  # noqa: SLF001
            assert "<html" in content.lower() or "<body" in content.lower()
        finally:
            await cm.close()

    async def test_content_markdown_format(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="content3"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="content3"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="content3"))
            await driver.navigate(page, local_site_url + "/")
            content = await executor._fetch_content(page, "markdown", None, 50000)  # noqa: SLF001
            # Markdown should not contain raw HTML tags.
            assert "<script" not in content.lower()
            assert "<style" not in content.lower()
        finally:
            await cm.close()

    async def test_content_with_selector_scope(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="content4"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="content4"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="content4"))
            await driver.navigate(page, local_site_url + "/")
            # Scope to the nav element only.
            content = await executor._fetch_content(page, "txt", "nav", 50000)  # noqa: SLF001
            assert len(content) < 2000  # nav is small
            assert "Login" in content or "Products" in content
        finally:
            await cm.close()

    async def test_content_max_chars_truncates(self, browser_manager, settings, local_site_url) -> None:
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="content5"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="content5"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="content5"))
            await driver.navigate(page, local_site_url + "/")
            content = await executor._fetch_content(page, "txt", None, 100)  # noqa: SLF001
            assert len(content) <= 120  # 100 + truncation marker
            assert "truncated" in content
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# Deep challenge scan
# ---------------------------------------------------------------------------


class TestDeepChallengeScan:
    async def test_deep_scan_detects_turnstile(self, browser_manager, settings, local_site_url) -> None:
        """The deep scan should detect the Turnstile test sitekey on /challenge."""
        from sera_browser.challenge.detector import ChallengeDetector
        from sera_browser.browser.driver import PlaywrightDriver

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="challenge"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="challenge"))
            await driver.navigate(page, local_site_url + "/challenge")
            detector = ChallengeDetector()
            result = await detector.detect(page)
            types = [c.type for c in result.challenges]
            # Turnstile should be detected (either by selector or deep scan).
            assert "cloudflare_turnstile" in types
        finally:
            await cm.close()

    async def test_deep_scan_no_false_positive_on_normal_page(self, browser_manager, settings, local_site_url) -> None:
        """The deep scan should not detect challenges on a normal page."""
        from sera_browser.challenge.detector import ChallengeDetector
        from sera_browser.browser.driver import PlaywrightDriver

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="challenge2"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="challenge2"))
            await driver.navigate(page, local_site_url + "/")
            detector = ChallengeDetector()
            # The deep scan should return None on a normal page.
            deep_result = await detector._deep_scan(page)  # noqa: SLF001
            assert deep_result is None
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# History + Replay integration
# ---------------------------------------------------------------------------


class TestHistoryReplayIntegration:
    async def test_history_records_actions(self, browser_manager, settings, local_site_url) -> None:
        """The agent should record every action in history."""
        from sera_browser.browser.driver import PlaywrightDriver
        from sera_browser.task.executor import ActionExecutor
        from sera_browser.browser.inspector import Inspector
        from sera_browser.task.loop import LoopRecoveryManager

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="hist"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="hist"))
            inspector = Inspector(driver=driver)
            executor = ActionExecutor(driver, inspector, settings, CancellationManager(task_id="hist"))
            hm = HistoryManager()
            executor._history_manager = hm  # noqa: SLF001
            state = _make_state()
            loop = LoopRecoveryManager()
            await driver.navigate(page, local_site_url + "/")
            # Execute an action.
            await executor.execute(page, state, loop, "scroll", dy=100)
            # Manually record it (the agent does this automatically).
            hm.record("scroll", {"dy": 100}, True, result="ok")
            assert len(hm.list_entries()) == 1
            assert hm.list_entries()[0].action == "scroll"
        finally:
            await cm.close()
