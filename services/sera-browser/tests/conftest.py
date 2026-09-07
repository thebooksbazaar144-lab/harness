"""Pytest fixtures: shared browser launch, local test site server, settings."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from sera_browser.browser.manager import BrowserManager
from sera_browser.config import Settings
from sera_browser.logging import configure_logging

from tests.fixtures.local_site.app import create_app as create_local_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def local_site_url() -> str:
    """Start the local FastAPI test site in a background thread + uvicorn."""
    import uvicorn

    app = create_local_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait until server is up.
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def settings(tmp_path, local_site_url) -> Settings:
    return Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        session_root=tmp_path / "sessions",
        screenshot_root=tmp_path / "screenshots",
        max_steps=20,
        log_level="INFO",
        log_format="text",
        network_idle_timeout_ms=2000,
    )


@pytest_asyncio.fixture
async def browser_manager(settings: Settings) -> BrowserManager:
    bm = BrowserManager(settings=settings)
    try:
        yield bm
    finally:
        try:
            await asyncio.wait_for(bm.shutdown(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            await bm.force_kill()


@pytest.fixture
def juice_shop_url() -> str | None:
    """Probe for a local Juice Shop on the default port (3000)."""
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:3000/", timeout=1.0).read(100)
        return "http://127.0.0.1:3000"
    except Exception:  # noqa: BLE001
        return None
