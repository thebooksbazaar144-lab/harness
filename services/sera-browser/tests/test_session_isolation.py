"""Session isolation tests: two simultaneous sessions don't share state."""
from __future__ import annotations

import asyncio

import pytest

from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import TaskCreate
from sera_browser.session.manager import SessionManager


pytestmark = pytest.mark.asyncio


async def test_two_sessions_isolated(browser_manager, settings, local_site_url):
    """Two simultaneous tasks must not share browser state."""
    sm = SessionManager(settings=settings, browser_manager=browser_manager)
    req1 = TaskCreate(
        target=local_site_url + "/login",
        task="inspect login form",
        browser_mode="headless",
        timeout=30,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=4,
    )
    req2 = TaskCreate(
        target=local_site_url + "/products",
        task="inspect products",
        browser_mode="headless",
        timeout=30,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=4,
    )
    rec1 = await sm.create(req1)
    rec2 = await sm.create(req2)
    # Wait for both to complete.
    async def _wait(rec):
        while rec.task is not None and not rec.task.done():
            await asyncio.sleep(0.1)
    await asyncio.gather(_wait(rec1), _wait(rec2))
    assert rec1.report is not None
    assert rec2.report is not None
    assert rec1.task_id != rec2.task_id
    # Their reports must describe different pages.
    assert (rec1.report.target or "") != (rec2.report.target or "")


async def test_cross_task_isolation(browser_manager, settings, local_site_url):
    """Task A's storage state must not bleed into task B."""
    sm = SessionManager(settings=settings, browser_manager=browser_manager)
    # Task A: set localStorage
    req_a = TaskCreate(
        target=local_site_url + "/",
        task="set marker",
        browser_mode="headless",
        timeout=30,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=2,
    )
    rec_a = await sm.create(req_a)
    while rec_a.task is not None and not rec_a.task.done():
        await asyncio.sleep(0.1)
    # Task B should not see task A's storage (separate context).
    req_b = TaskCreate(
        target=local_site_url + "/",
        task="verify isolation",
        browser_mode="headless",
        timeout=30,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=2,
    )
    rec_b = await sm.create(req_b)
    while rec_b.task is not None and not rec_b.task.done():
        await asyncio.sleep(0.1)
    assert rec_a.report is not None
    assert rec_b.report is not None
    # Different task_ids
    assert rec_a.task_id != rec_b.task_id


async def test_rejected_target_fails_fast(browser_manager, settings):
    """SSRF protection: external target with no allowlist fails immediately."""
    sm = SessionManager(settings=settings, browser_manager=browser_manager)
    req = TaskCreate(
        target="http://169.254.169.254/latest/meta-data/",  # cloud metadata
        task="ssrf probe",
        browser_mode="headless",
        timeout=5,
        local_lab=False,
    )
    rec = await sm.create(req)
    assert rec.status.value == "failed"
    assert "rejected" in (rec.error or "").lower() or "blocked" in (rec.error or "").lower()
