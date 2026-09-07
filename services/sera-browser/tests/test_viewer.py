"""Tests for the real-view browser viewer (human-takeoverable browser).

Covers:
- BrowserViewer construction + start/stop
- Client connect/disconnect + broadcast
- Screenshot capture (base64 data URL)
- Step broadcasting (with screenshot)
- Human takeover request + resume
- Viewer stats
- SessionManager request_takeover / resume_task
- API endpoints (viewer status, human_takeover, resume, screenshot)
- CDP remote debugging flag
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera_browser.browser.viewer import BrowserViewer, StreamEvent, ViewerStats
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings


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


# ---------------------------------------------------------------------------
# BrowserViewer unit tests
# ---------------------------------------------------------------------------


class TestBrowserViewer:
    async def test_start_sets_page_and_cdp_port(self) -> None:
        viewer = BrowserViewer("test-1", session_dir="/tmp")
        assert viewer._page is None
        assert viewer.cdp_url is None
        # Simulate start (we pass a dummy "page" — the viewer doesn't use it
        # until broadcast_step, which we skip here).
        viewer.start(page=None, cdp_port=9222)  # type: ignore[arg-type]
        assert viewer._cdp_port == 9222
        assert viewer.cdp_url == "http://127.0.0.1:9222"

    async def test_cdp_url_none_when_no_port(self) -> None:
        viewer = BrowserViewer("test-2", session_dir="/tmp")
        viewer.start(page=None, cdp_port=None)  # type: ignore[arg-type]
        assert viewer.cdp_url is None

    async def test_add_remove_client(self) -> None:
        viewer = BrowserViewer("test-3", session_dir="/tmp")
        q1 = viewer.add_client()
        q2 = viewer.add_client()
        assert len(viewer._clients) == 2
        assert viewer.stats.clients_connected == 2
        assert viewer.stats.clients_current == 2
        viewer.remove_client(q1)
        assert len(viewer._clients) == 1
        assert viewer.stats.clients_current == 1

    async def test_broadcast_sends_to_all_clients(self) -> None:
        viewer = BrowserViewer("test-4", session_dir="/tmp")
        q1 = viewer.add_client()
        q2 = viewer.add_client()
        event = StreamEvent(type="step", data={"step": 1})
        await viewer._broadcast(event)
        # Both clients should have the event.
        assert not q1.empty()
        assert not q2.empty()
        e1 = q1.get_nowait()
        assert e1.type == "step"
        assert e1.data["step"] == 1

    async def test_broadcast_drops_on_full_queue(self) -> None:
        viewer = BrowserViewer("test-5", session_dir="/tmp")
        q = viewer.add_client()  # maxsize=100
        # Fill the queue.
        for i in range(100):
            await viewer._broadcast(StreamEvent(type="step", data={"step": i}))
        assert q.full()
        # One more should drop the oldest + add the newest.
        await viewer._broadcast(StreamEvent(type="step", data={"step": 999}))
        assert q.full()  # still full (dropped oldest=0, added newest=999)
        # Drain the queue; the last item should be step 999.
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert items[-1].data["step"] == 999  # newest is last
        assert items[0].data["step"] == 1  # oldest was dropped (step 0 → now step 1)

    async def test_takeover_request_sets_active(self) -> None:
        viewer = BrowserViewer("test-6", session_dir="/tmp")
        assert not viewer.is_takeover_active
        await viewer.request_takeover("human wants to log in")
        assert viewer.is_takeover_active
        assert viewer._takeover_reason == "human wants to log in"
        assert viewer.stats.takeovers_requested == 1

    async def test_resume_clears_takeover(self) -> None:
        viewer = BrowserViewer("test-7", session_dir="/tmp")
        await viewer.request_takeover("test")
        assert viewer.is_takeover_active
        viewer.resume()
        assert not viewer.is_takeover_active

    async def test_wait_for_resume_returns_true_on_signal(self) -> None:
        viewer = BrowserViewer("test-8", session_dir="/tmp")
        await viewer.request_takeover("test")
        # Signal resume after a short delay.
        async def _delayed_resume():
            await asyncio.sleep(0.1)
            viewer.resume()
        asyncio.create_task(_delayed_resume())
        result = await viewer.wait_for_resume(timeout_s=2.0)
        assert result is True
        assert viewer.stats.takeovers_completed == 1

    async def test_wait_for_resume_returns_false_on_timeout(self) -> None:
        viewer = BrowserViewer("test-9", session_dir="/tmp")
        await viewer.request_takeover("test")
        result = await viewer.wait_for_resume(timeout_s=0.2)
        assert result is False
        assert viewer.stats.takeovers_completed == 1

    async def test_stream_event_to_json(self) -> None:
        event = StreamEvent(type="step", data={"url": "http://x/", "step": 3})
        js = event.to_json()
        parsed = json.loads(js)
        assert parsed["type"] == "step"
        assert parsed["data"]["url"] == "http://x/"
        assert parsed["data"]["step"] == 3
        assert "timestamp" in parsed

    async def test_stop_clears_clients(self) -> None:
        viewer = BrowserViewer("test-10", session_dir="/tmp")
        q = viewer.add_client()
        await viewer.stop()
        assert len(viewer._clients) == 0


# ---------------------------------------------------------------------------
# Viewer integration with real browser
# ---------------------------------------------------------------------------


class TestViewerIntegration:
    async def test_broadcast_step_with_screenshot(
        self, browser_manager, settings, local_site_url
    ) -> None:
        """broadcast_step should capture a screenshot + stream to clients."""
        from sera_browser.browser.driver import PlaywrightDriver

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="viewer"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="viewer"))
            await driver.navigate(page, local_site_url + "/")
            viewer = BrowserViewer("test-int", session_dir="/tmp")
            viewer.start(page, cdp_port=None)
            q = viewer.add_client()
            await viewer.broadcast_step(
                page, step_num=1, action="navigate", result="ok", detail="navigated",
            )
            # The client should have received a step event.
            event = await asyncio.wait_for(q.get(), timeout=2.0)
            assert event.type == "step"
            assert event.data["step"] == 1
            assert event.data["action"] == "navigate"
            assert event.data["url"].startswith(local_site_url)
            # Screenshot should be present as a base64 data URL.
            assert "screenshot" in event.data
            assert event.data["screenshot"].startswith("data:image/jpeg;base64,")
            assert viewer.stats.steps_streamed == 1
            assert viewer.stats.screenshots_streamed == 1
        finally:
            await cm.close()

    async def test_capture_screenshot_returns_png(
        self, browser_manager, settings, local_site_url
    ) -> None:
        from sera_browser.browser.driver import PlaywrightDriver

        handle = await browser_manager.launch()
        cm = await browser_manager.new_context_manager(CancellationManager(task_id="shot"))
        page = await cm.open(handle.browser)
        try:
            driver = PlaywrightDriver(settings, CancellationManager(task_id="shot"))
            await driver.navigate(page, local_site_url + "/")
            viewer = BrowserViewer("test-shot", session_dir="/tmp")
            viewer.start(page)
            png = await viewer.capture_screenshot(page)
            assert png is not None
            assert len(png) > 1000  # PNG header + data
            assert png[:4] == b"\x89PNG"  # PNG magic
        finally:
            await cm.close()


# ---------------------------------------------------------------------------
# SessionManager viewer integration
# ---------------------------------------------------------------------------


class TestSessionManagerViewer:
    async def test_request_takeover_without_viewer_returns_false(
        self, browser_manager, settings, local_site_url
    ) -> None:
        """When viewer is disabled, request_takeover returns False."""
        from sera_browser.models import TaskCreate
        from sera_browser.session.manager import SessionManager

        sm = SessionManager(settings=settings, browser_manager=browser_manager)
        req = TaskCreate(
            target=local_site_url + "/",
            task="test",
            browser_mode="headless",
            timeout=10,
            local_lab=True,
            allowed_targets=["127.0.0.1", "localhost"],
            max_steps=2,
        )
        rec = await sm.create(req)
        # Viewer is not enabled → request_takeover should return False.
        ok = await sm.request_takeover(rec.task_id, "test")
        assert ok is False

    async def test_resume_without_viewer_returns_false(
        self, browser_manager, settings, local_site_url
    ) -> None:
        from sera_browser.models import TaskCreate
        from sera_browser.session.manager import SessionManager

        sm = SessionManager(settings=settings, browser_manager=browser_manager)
        req = TaskCreate(
            target=local_site_url + "/",
            task="test",
            browser_mode="headless",
            timeout=10,
            local_lab=True,
            allowed_targets=["127.0.0.1", "localhost"],
            max_steps=2,
        )
        rec = await sm.create(req)
        ok = sm.resume_task(rec.task_id)
        assert ok is False


# ---------------------------------------------------------------------------
# Config settings
# ---------------------------------------------------------------------------


class TestViewerConfig:
    def test_viewer_disabled_by_default(self) -> None:
        s = Settings()
        assert not s.enable_viewer

    def test_viewer_enabled_via_setting(self) -> None:
        s = Settings(enable_viewer=True)
        assert s.enable_viewer
        assert s.viewer_cdp_port == 9222
        assert s.viewer_stream_screenshots is True
        assert s.viewer_screenshot_quality == 60
        assert s.viewer_takeover_timeout_s == 300
        assert s.viewer_force_headed is True

    def test_viewer_config_overrides(self) -> None:
        s = Settings(
            enable_viewer=True,
            viewer_cdp_port=9333,
            viewer_stream_screenshots=False,
            viewer_screenshot_quality=80,
            viewer_takeover_timeout_s=600,
            viewer_force_headed=False,
        )
        assert s.viewer_cdp_port == 9333
        assert not s.viewer_stream_screenshots
        assert s.viewer_screenshot_quality == 80
        assert s.viewer_takeover_timeout_s == 600
        assert not s.viewer_force_headed


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestViewerAPI:
    def test_viewer_status_endpoint(self, tmp_path, local_site_url) -> None:
        from fastapi.testclient import TestClient
        from sera_browser.api.app import create_app

        settings = _make_settings()
        app = create_app(settings)
        with TestClient(app) as client:
            # Health should show viewer_enabled: False.
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json()["viewer_enabled"] is False

    def test_viewer_status_when_enabled(self, tmp_path, local_site_url) -> None:
        from fastapi.testclient import TestClient
        from sera_browser.api.app import create_app

        settings = _make_settings(enable_viewer=True)
        app = create_app(settings)
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.json()["viewer_enabled"] is True

    def test_human_takeover_without_task_returns_404(self, tmp_path, local_site_url) -> None:
        from fastapi.testclient import TestClient
        from sera_browser.api.app import create_app

        settings = _make_settings(enable_viewer=True)
        app = create_app(settings)
        with TestClient(app) as client:
            r = client.post("/tasks/nonexistent-task/human_takeover")
            assert r.status_code == 404

    def test_resume_without_task_returns_404(self, tmp_path, local_site_url) -> None:
        from fastapi.testclient import TestClient
        from sera_browser.api.app import create_app

        settings = _make_settings(enable_viewer=True)
        app = create_app(settings)
        with TestClient(app) as client:
            r = client.post("/tasks/nonexistent-task/resume")
            assert r.status_code == 404

    def test_screenshot_without_task_returns_404(self, tmp_path, local_site_url) -> None:
        from fastapi.testclient import TestClient
        from sera_browser.api.app import create_app

        settings = _make_settings(enable_viewer=True)
        app = create_app(settings)
        with TestClient(app) as client:
            r = client.get("/tasks/nonexistent-task/screenshot")
            assert r.status_code == 404
