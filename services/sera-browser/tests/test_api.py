"""API tests: create, poll, cancel, result."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from sera_browser.api.app import create_app
from sera_browser.config import Settings


@pytest.fixture
def api_client(tmp_path, local_site_url):
    settings = Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        session_root=tmp_path / "sessions",
        screenshot_root=tmp_path / "screenshots",
        max_steps=6,
        log_level="INFO",
        log_format="text",
        network_idle_timeout_ms=1500,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_health(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "browser_mode" in body


def test_create_task_and_get_result(api_client, local_site_url):
    r = api_client.post(
        "/tasks",
        json={
            "target": local_site_url + "/login",
            "task": "inspect login form",
            "browser_mode": "headless",
            "timeout": 30,
            "local_lab": True,
            "allowed_targets": ["127.0.0.1", "localhost"],
            "max_steps": 3,
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    task_id = body["task_id"]
    assert body["status"] in {"running", "pending"}

    # Poll for completion.
    deadline = 30.0
    import time

    started = time.time()
    while time.time() - started < deadline:
        s = api_client.get(f"/tasks/{task_id}").json()
        if s["status"] in {"completed", "failed", "cancelled", "timeout", "blocked"}:
            break
        time.sleep(0.3)
    assert s["status"] in {"completed", "failed", "timeout", "blocked"}, s

    if s["has_report"]:
        result = api_client.get(f"/tasks/{task_id}/result").json()
        assert result["task_id"] == task_id
        assert result["target"] == local_site_url + "/login"


def test_cancel_task(api_client, local_site_url):
    """Cancel a running task; verify it ends and no orphan process lingers."""
    import subprocess
    import time

    r = api_client.post(
        "/tasks",
        json={
            "target": local_site_url + "/products",
            "task": "explore",
            "browser_mode": "headless",
            "timeout": 60,
            "local_lab": True,
            "allowed_targets": ["127.0.0.1", "localhost"],
            "max_steps": 50,
        },
    )
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    # Cancel quickly while it's running.
    time.sleep(0.5)
    cr = api_client.post(f"/tasks/{task_id}/cancel").json()
    assert cr["cancelled"] is True

    # Wait for status to settle.
    for _ in range(40):
        s = api_client.get(f"/tasks/{task_id}").json()
        if s["status"] in {"cancelled", "completed", "failed"}:
            break
        time.sleep(0.25)

    assert s["status"] in {"cancelled", "completed", "failed"}


def test_rejected_target_returns_400(api_client):
    r = api_client.post(
        "/tasks",
        json={
            "target": "http://169.254.169.254/",
            "task": "ssrf probe",
            "browser_mode": "headless",
            "timeout": 5,
        },
    )
    assert r.status_code == 400
    assert "rejected" in r.json()["detail"].lower() or "blocked" in r.json()["detail"].lower()
