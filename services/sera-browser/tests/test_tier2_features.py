"""Tests for Tier 2 advanced features.

Covers:
- Stealth layer (fingerprint randomization, init scripts, context kwargs)
- Set-of-Mark vision fallback (annotation, ID resolution, PIL fallback)
- Structured extraction (schema validation, prompt building, response parsing)
- flash_mode (shortened prompts, no reflection)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.browser.stealth import (
    StealthConfig,
    StealthFingerprint,
    StealthLayer,
)
from sera_browser.browser.som import (
    MarkedElement,
    SetOfMarkAnnotator,
    SoMResult,
)
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import BrowserState, InteractiveElement
from sera_browser.task.extraction import (
    ExtractionResult,
    build_extraction_prompt,
    parse_extraction_response,
    validate_against_schema,
)
from sera_browser.task.llm_client import MockLLMClient
from sera_browser.task.llm_planner import LLMTaskPlanner
from sera_browser.task.loop import LoopRecoveryManager
from sera_browser.task.planner import TaskPlanner


# ---------------------------------------------------------------------------
# Stealth layer
# ---------------------------------------------------------------------------


class TestStealthLayer:
    def test_disabled_by_default(self) -> None:
        stealth = StealthLayer()
        assert not stealth.config.enabled

    def test_choose_fingerprint_randomizes(self) -> None:
        stealth = StealthLayer(StealthConfig(level="medium"))
        fp1 = stealth.choose_fingerprint()
        fp2 = stealth.choose_fingerprint()
        assert fp1.user_agent
        assert fp1.viewport["width"] > 0
        assert fp1.timezone
        assert fp1.locale
        # At least one field should differ over many draws (probabilistic).
        fps = [stealth.choose_fingerprint() for _ in range(20)]
        uas = {f.user_agent for f in fps}
        assert len(uas) > 1, "user agent should vary"

    def test_choose_fingerprint_respects_fixed_values(self) -> None:
        stealth = StealthLayer(StealthConfig(
            level="light",
            user_agent="TestUA/1.0",
            viewport={"width": 800, "height": 600},
            timezone="UTC",
            locale="en-US",
        ))
        fp = stealth.choose_fingerprint()
        assert fp.user_agent == "TestUA/1.0"
        assert fp.viewport == {"width": 800, "height": 600}
        assert fp.timezone == "UTC"
        assert fp.locale == "en-US"

    def test_context_kwargs_when_disabled(self) -> None:
        stealth = StealthLayer(StealthConfig(level="off"))
        fp = stealth.choose_fingerprint()
        kwargs = stealth.context_kwargs(fp)
        assert kwargs == {}

    def test_context_kwargs_when_enabled(self) -> None:
        stealth = StealthLayer(StealthConfig(
            level="medium",
            user_agent="TestUA/1.0",
            viewport={"width": 800, "height": 600},
            timezone="America/New_York",
            locale="en-US",
        ))
        fp = stealth.choose_fingerprint()
        kwargs = stealth.context_kwargs(fp)
        assert "user_agent" in kwargs
        assert "viewport" in kwargs
        assert "timezone_id" in kwargs
        assert "locale" in kwargs
        assert kwargs["user_agent"] == "TestUA/1.0"

    def test_context_kwargs_with_proxy(self) -> None:
        stealth = StealthLayer(StealthConfig(
            level="light",
            proxy="http://user:pass@proxy.example.com:8080",
        ))
        fp = stealth.choose_fingerprint()
        kwargs = stealth.context_kwargs(fp)
        assert "proxy" in kwargs
        assert kwargs["proxy"]["server"] == "http://proxy.example.com:8080"
        assert kwargs["proxy"]["username"] == "user"
        assert kwargs["proxy"]["password"] == "pass"

    @pytest.mark.asyncio
    async def test_apply_injects_init_script(self, browser_manager, settings, local_site_url) -> None:
        """Verify stealth.apply() doesn't raise on a real page."""
        from sera_browser.browser.stealth import StealthConfig, StealthLayer

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="stealth-test"))
        page = await cm.open(handle.browser)
        try:
            stealth = StealthLayer(StealthConfig(level="medium"))
            fp = stealth.choose_fingerprint()
            await stealth.apply(page, fp)
            # Navigate to verify init scripts don't break the page.
            await page.goto(local_site_url + "/")
            assert page.url.startswith(local_site_url)
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# Set-of-Mark vision fallback
# ---------------------------------------------------------------------------


class TestSetOfMarkAnnotator:
    def test_resolve_id(self) -> None:
        annotator = SetOfMarkAnnotator()
        # Build a fake SoMResult.
        el = InteractiveElement(ref="button#1:Login", role="button", name="Login", tag="button")
        marked = MarkedElement(index=5, element=el, bbox={"x": 10, "y": 20, "width": 50, "height": 30})
        result = SoMResult(image_png=b"fake", marked_elements=[marked])
        # Resolve ID 5.
        found = annotator.resolve_id(result, 5)
        assert found is not None
        assert found.element.ref == "button#1:Login"
        # Resolve non-existent ID.
        assert annotator.resolve_id(result, 99) is None

    @pytest.mark.asyncio
    async def test_annotate_with_empty_elements(self, browser_manager, settings, local_site_url) -> None:
        """Annotating with no elements should return the raw screenshot."""
        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="som-test"))
        page = await cm.open(handle.browser)
        try:
            await page.goto(local_site_url + "/")
            annotator = SetOfMarkAnnotator()
            result = await annotator.annotate(page, [])
            assert result.image_png  # non-empty
            assert len(result.marked_elements) == 0
        finally:
            await cm.close()

    @pytest.mark.asyncio
    async def test_annotate_returns_marked_elements(self, browser_manager, settings, local_site_url) -> None:
        """Annotating with real elements should return bounding boxes."""
        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="som-test2"))
        page = await cm.open(handle.browser)
        try:
            await page.goto(local_site_url + "/login")
            annotator = SetOfMarkAnnotator()
            # Build elements from the page.
            from sera_browser.browser.driver import PlaywrightDriver
            driver = PlaywrightDriver(settings, CancellationManager(task_id="som-test2"))
            snap = await driver.snapshot(page)
            result = await annotator.annotate(page, snap.elements)
            # Should have at least some marked elements (the login page has buttons + inputs).
            assert len(result.marked_elements) > 0
            # Each marked element should have a valid bounding box.
            for m in result.marked_elements:
                assert m.bbox["width"] > 0
                assert m.bbox["height"] > 0
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# Structured extraction
# ---------------------------------------------------------------------------


class TestStructuredExtraction:
    def test_validate_no_schema_passes(self) -> None:
        ok, msg = validate_against_schema({"a": 1}, None)
        assert ok

    def test_validate_simple_object_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        ok, msg = validate_against_schema({"name": "Alice", "age": 30}, schema)
        assert ok, msg

    def test_validate_missing_required_field_fails(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        ok, msg = validate_against_schema({"age": 30}, schema)
        assert not ok
        assert "missing required field 'name'" in msg

    def test_validate_wrong_type_fails(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }
        ok, msg = validate_against_schema({"name": 123}, schema)
        assert not ok
        assert "expected string" in msg

    def test_validate_array_with_items(self) -> None:
        schema = {
            "type": "array",
            "items": {"type": "string"},
        }
        ok, _ = validate_against_schema(["a", "b", "c"], schema)
        assert ok
        ok, msg = validate_against_schema(["a", 123, "c"], schema)
        assert not ok
        assert "expected string" in msg

    def test_validate_enum(self) -> None:
        schema = {"type": "string", "enum": ["red", "green", "blue"]}
        ok, _ = validate_against_schema("red", schema)
        assert ok
        ok, msg = validate_against_schema("purple", schema)
        assert not ok
        assert "not in enum" in msg

    def test_validate_bool_as_integer_rejected(self) -> None:
        schema = {"type": "integer"}
        ok, msg = validate_against_schema(True, schema)
        assert not ok
        assert "got boolean" in msg

    def test_build_extraction_prompt_includes_schema(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        prompt = build_extraction_prompt("page content", "extract names", schema)
        assert "extract names" in prompt
        assert "name" in prompt
        assert "page content" in prompt

    def test_parse_extraction_response_valid_json(self) -> None:
        raw = '{"name": "Alice", "age": 30}'
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        }
        result = parse_extraction_response(raw, schema)
        assert result.ok
        assert result.data["name"] == "Alice"

    def test_parse_extraction_response_markdown_fenced(self) -> None:
        raw = '```json\n{"name": "Bob"}\n```'
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = parse_extraction_response(raw, schema)
        assert result.ok
        assert result.data["name"] == "Bob"

    def test_parse_extraction_response_invalid_json(self) -> None:
        result = parse_extraction_response("not json", None)
        assert not result.ok
        assert "no JSON" in result.error

    def test_parse_extraction_response_schema_mismatch(self) -> None:
        raw = '{"name": 123}'  # name should be string
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = parse_extraction_response(raw, schema)
        assert not result.ok
        assert "schema validation failed" in result.error


# ---------------------------------------------------------------------------
# flash_mode
# ---------------------------------------------------------------------------


class TestFlashMode:
    @pytest.mark.asyncio
    async def test_flash_mode_shortens_prompt(self) -> None:
        """In flash_mode, the user prompt should be shorter (no reflection)."""
        state = BrowserState(task_id="t", target="http://x/")
        state.current_url = "http://x/"
        state.dom_hash = "abc"

        # Normal mode.
        cm1 = CancellationManager(task_id="t1")
        settings_normal = Settings(max_steps=5, flash_mode=False)
        client_normal = MockLLMClient(responses=['{"action": "finish", "args": {}}'])
        planner_normal = LLMTaskPlanner(
            settings=settings_normal,
            client=client_normal,
            cancellation=cm1,
            objective="test",
            deterministic=TaskPlanner(settings_normal),
        )
        await planner_normal.next_step(state, LoopRecoveryManager())
        prompt_normal = client_normal.calls[0]["user"]

        # Flash mode.
        cm2 = CancellationManager(task_id="t2")
        settings_flash = Settings(max_steps=5, flash_mode=True)
        client_flash = MockLLMClient(responses=['{"action": "finish", "args": {}}'])
        planner_flash = LLMTaskPlanner(
            settings=settings_flash,
            client=client_flash,
            cancellation=cm2,
            objective="test",
            deterministic=TaskPlanner(settings_flash),
        )
        await planner_flash.next_step(state, LoopRecoveryManager())
        prompt_flash = client_flash.calls[0]["user"]

        # Flash mode prompt should be shorter.
        assert len(prompt_flash) < len(prompt_normal)
        # Flash mode should not include the "thinking" instruction.
        assert "thinking" not in prompt_flash.lower()
