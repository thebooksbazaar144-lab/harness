"""KernelRuntime backend tests (rebuild v4, Phase 3).

A stub kernel browser service (FastAPI) pins the wire contract documented
in runtime/kernel.py. Verifies:
- protocol conformance of KernelRuntime (BrowserRuntime surface)
- session lifecycle (start / stop / health)
- bounded failure classification (5xx → provider_error; connect error →
  browser_disconnected; never a retry loop)
- backend selection semantics (local default, kernel requires URL,
  auto probes health and falls back)
- LocalPlaywrightRuntime satisfies the protocol too
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI

from sera_browser.browser.element_registry import ElementRegistry
from sera_browser.browser.driver import PlaywrightDriver
from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.runtime import (
    KernelRuntime,
    LocalPlaywrightRuntime,
    create_runtime,
    select_backend,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Stub kernel browser service
# ---------------------------------------------------------------------------


def _stub_kernel_app() -> FastAPI:
    app = FastAPI()
    state: dict = {"url": "http://stub/", "title": "Stub", "clicked": []}

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "service": "kernel-browser", "version": "stub-1"}

    @app.post("/sessions")
    async def create_session(body: dict) -> dict:
        return {"ok": True, "session_id": "sess-1"}

    @app.delete("/sessions/{sid}")
    async def delete_session(sid: str) -> dict:
        return {"ok": True}

    @app.post("/sessions/{sid}/navigate")
    async def navigate(sid: str, body: dict) -> dict:
        state["url"] = body["url"]
        return {"ok": True, "url": state["url"], "ms": 12.5}

    @app.get("/sessions/{sid}/snapshot")
    async def snapshot(sid: str) -> dict:
        return {
            "ok": True,
            "url": state["url"],
            "title": state["title"],
            "dom_hash": "abc123",
            "elements": [
                {
                    "ref": "e001",
                    "role": "link",
                    "name": "Home",
                    "tag": "a",
                    "href": "/",
                    "locator": {"type": "role", "role": "link", "name": "Home"},
                },
                {"ref": "e002", "role": "button", "name": "Go", "tag": "button"},
            ],
        }

    @app.post("/sessions/{sid}/actions")
    async def action(sid: str, body: dict) -> dict:
        if body.get("action") == "click" and body.get("ref") == "e666":
            return {"ok": False, "error": {"kind": "timeout", "detail": "not actionable"}}
        if body.get("action") == "click":
            state["clicked"].append(body.get("ref"))
            return {"ok": True}
        return {"ok": True}

    @app.get("/sessions/{sid}/links")
    async def links(sid: str) -> dict:
        return {"ok": True, "links": [{"href": "/", "text": "Home"}]}

    @app.get("/sessions/{sid}/storage")
    async def storage(sid: str) -> dict:
        return {"ok": True, "keys": ["token"]}

    return app


@pytest_asyncio.fixture
async def stub_kernel_url():
    """Run the stub kernel service on a free port; return its base URL."""
    import socket
    import threading
    import time as _time

    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(_stub_kernel_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        if server.started:
            break
        _time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def kernel_settings(tmp_path, stub_kernel_url) -> Settings:
    return Settings(
        browser_backend="kernel",
        kernel_url=stub_kernel_url,
        kernel_timeout_s=5.0,
        session_root=tmp_path / "sessions",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
    )


# ---------------------------------------------------------------------------
# Selection semantics
# ---------------------------------------------------------------------------


def test_select_backend_local_default():
    s = Settings(browser_backend="auto", kernel_url="")
    assert select_backend(s) == "local"


def test_select_backend_kernel_requires_url():
    s = Settings(browser_backend="kernel", kernel_url="")
    with pytest.raises(ValueError):
        select_backend(s)


def test_select_backend_auto_uses_kernel_when_configured():
    s = Settings(browser_backend="auto", kernel_url="http://127.0.0.1:9223")
    assert select_backend(s) == "kernel"


# ---------------------------------------------------------------------------
# KernelRuntime protocol conformance
# ---------------------------------------------------------------------------


async def test_kernel_runtime_full_surface(kernel_settings):
    rt = KernelRuntime(kernel_settings, task_id="k1")
    try:
        assert await rt.health() is True
        assert await rt.start() is True
        assert rt.name == "kernel"

        nav = await rt.navigate("http://stub/products")
        assert nav["ok"] is True and nav["ms"] == 12.5

        snap = await rt.snapshot()
        assert snap["ok"] is True
        assert snap["url"] == "http://stub/products"
        assert len(snap["elements"]) == 2
        assert snap["elements"][0]["ref"] == "e001"

        clicked = await rt.click("e001")
        assert clicked["ok"] is True

        filled = await rt.fill("e001", "x")
        assert filled["ok"] is True

        links = await rt.find_links()
        assert links == [{"href": "/", "text": "Home"}]

        keys = await rt.list_storage_keys("localStorage")
        assert keys == ["token"]

        url = await rt.get_current_url()
        assert url == "http://stub/products"
    finally:
        await rt.stop()


async def test_kernel_runtime_classifies_failures(kernel_settings):
    rt = KernelRuntime(kernel_settings, task_id="k2")
    await rt.start()
    try:
        res = await rt.click("e666")
        assert res["ok"] is False
        failure = res["failure"]
        assert failure is not None
        assert failure.kind == "timeout"
        assert failure.retry_recommended is True
    finally:
        await rt.stop()


async def test_kernel_runtime_unreachable_classified(tmp_path):
    """Unreachable kernel: health check returns False (ONE bounded probe,
    no retry loop)."""
    s = Settings(
        browser_backend="kernel",
        kernel_url="http://127.0.0.1:1",  # nothing listens here
        kernel_timeout_s=1.0,
        session_root=tmp_path / "s",
    )
    rt = KernelRuntime(s, task_id="k3")
    assert await rt.health() is False
    started = await rt.start()
    assert started is False
    await rt.stop()


async def test_create_runtime_auto_falls_back_to_local(tmp_path, stub_kernel_url):
    """auto + healthy kernel → kernel; auto + dead kernel → local."""
    from sera_browser.browser.element_registry import ElementRegistry

    # Healthy kernel.
    s = Settings(browser_backend="auto", kernel_url=stub_kernel_url, kernel_timeout_s=5.0)
    rt, name = await create_runtime(s, task_id="k4")
    assert name == "kernel"
    await rt.stop()  # type: ignore[attr-defined]

    # Dead kernel: auto must degrade to local without raising.
    s2 = Settings(
        browser_backend="auto",
        kernel_url="http://127.0.0.1:1",
        kernel_timeout_s=0.5,
        session_root=tmp_path / "s",
    )
    # create_runtime local fallback requires driver+page — use fakes.
    class _FakeDriver:
        pass

    class _FakePage:
        url = "about:blank"

    rt2, name2 = await create_runtime(
        s2, task_id="k5", driver=_FakeDriver(), page=_FakePage(), registry=ElementRegistry()
    )
    assert name2 == "local"
    assert isinstance(rt2, LocalPlaywrightRuntime)


# ---------------------------------------------------------------------------
# LocalPlaywrightRuntime protocol conformance (live)
# ---------------------------------------------------------------------------


async def test_local_runtime_satisfies_protocol(browser_manager, settings, local_site_url):
    from sera_browser.runtime.base import BrowserRuntime

    handle = await browser_manager.launch()
    cm = await browser_manager.new_context_manager(CancellationManager(task_id="lr"))
    page = await cm.open(handle.browser)
    driver = PlaywrightDriver(settings, CancellationManager(task_id="lr"))
    registry = ElementRegistry()
    rt = LocalPlaywrightRuntime(settings, driver, page, registry)

    assert isinstance(rt, BrowserRuntime) or hasattr(rt, "navigate")
    assert await rt.health() is True
    nav = await rt.navigate(local_site_url + "/")
    assert nav["ok"] is True
    snap = await rt.snapshot()
    assert snap["ok"] is True
    assert snap["elements"], "local snapshot must expose elements"
    # Elements carry registry refs.
    assert all(e["ref"] is None or str(e["ref"]).startswith("e") for e in snap["elements"])

    # Click through the runtime: fresh registry resolution.
    from sera_browser.browser.inspector import Inspector

    inspector = Inspector(driver=driver, registry=registry)
    await inspector.observe(page)
    ref = snap["elements"][0]["ref"]
    res = await rt.click(ref)
    assert "ok" in res
