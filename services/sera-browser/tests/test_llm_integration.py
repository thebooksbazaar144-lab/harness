"""End-to-end integration: LLM planner drives a real browser task.

Uses MockLLMClient to make the LLM "choose" a deterministic sequence of
actions against the local test site. Verifies the full flow:

    task → observe → LLM chooses → validate → Playwright executes
    → observe again → LLM chooses finish → structured report
"""
from __future__ import annotations

import asyncio
import json

import pytest

from sera_browser.browser.manager import BrowserManager
from sera_browser.cancel import CancellationManager
from sera_browser.config import Settings
from sera_browser.models import TaskCreate
from sera_browser.session.manager import SessionManager
from sera_browser.task.llm_client import MockLLMClient
from sera_browser.task.llm_planner import LLMTaskPlanner
from sera_browser.task.planner import TaskPlanner
from sera_browser.browser.manager import BrowserManager


pytestmark = pytest.mark.asyncio


@pytest.fixture
def llm_settings(tmp_path, local_site_url) -> Settings:
    return Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost",
        session_root=tmp_path / "sessions",
        screenshot_root=tmp_path / "screenshots",
        max_steps=8,
        llm_provider="zai",  # required for _build_planner to construct LLM planner
        llm_timeout_s=5.0,
        log_level="INFO",
        log_format="text",
        network_idle_timeout_ms=1500,
    )


async def test_llm_planner_drives_full_task(
    browser_manager: BrowserManager, llm_settings: Settings, local_site_url: str
) -> None:
    """The LLM planner (via MockLLMClient) drives a real browser task.

    Sequence of LLM-chosen actions:
      1. navigate to /login
      2. fill the Username textbox
      3. finish
    """
    # The mock client returns these responses in order. The first call sees
    # the initial state (no current_url yet) — but our deterministic
    # fallback handles the very first navigate. So we feed the mock the
    # actions it should pick AFTER the initial navigation + observation.
    mock_client = MockLLMClient(
        responses=[
            # After initial nav + observe: navigate to /login.
            json.dumps(
                {
                    "thinking": "navigate to the login page",
                    "action": "navigate",
                    "args": {"url": local_site_url + "/login"},
                    "reason": "explore auth",
                }
            ),
            # After observing the login page: fill the username field.
            # We can't know the exact ref ahead of time, so we use a
            # callable that inspects the user prompt.
            lambda system, user: _pick_fill_response(user, "Username"),
            # Then finish.
            json.dumps(
                {
                    "thinking": "objective satisfied",
                    "action": "finish",
                    "args": {},
                    "reason": "demo complete",
                }
            ),
        ]
    )

    # Inject the mock client via the BrowserAgent constructor.
    from sera_browser.task.agent import BrowserAgent

    # Patch SessionManager to use our agent with the mock client.
    # The simplest path: construct a BrowserAgent directly and run it.
    agent = BrowserAgent(
        settings=llm_settings,
        browser_manager=browser_manager,
        llm_client=mock_client,
    )
    from sera_browser.task.agent import TaskConfig

    cfg = TaskConfig(
        task_id="test-llm-task",
        target=local_site_url + "/",
        objective="Inspect the login form briefly, then finish.",
        browser_mode="headless",
        timeout_s=60,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=8,
    )
    cancellation = CancellationManager(task_id="test-llm-task")
    result = await agent.run(cfg, cancellation)

    assert result.report is not None, f"error={result.error}"
    report = result.report
    assert report.status == "completed"
    assert report.target == local_site_url + "/"
    # The LLM should have made at least 1 successful call.
    # (We can't assert exact count because the deterministic planner may
    # have handled the initial navigate.)
    assert report.steps >= 1


def _pick_fill_response(user_prompt: str, field_name: str) -> str:
    """Parse the user prompt's JSON state view and return a fill action
    targeting the first textbox whose name matches `field_name`."""
    start = user_prompt.find("{")
    end = user_prompt.rfind("}")
    if start == -1 or end == -1:
        # Fallback: finish
        return json.dumps({"action": "finish", "args": {}})
    try:
        view = json.loads(user_prompt[start : end + 1])
    except json.JSONDecodeError:
        return json.dumps({"action": "finish", "args": {}})
    for el in view.get("interactive_elements", []):
        if el.get("role") == "textbox" and field_name.lower() in el.get("name", "").lower():
            return json.dumps(
                {
                    "thinking": f"fill the {field_name} field",
                    "action": "fill",
                    "args": {"ref": el["ref"], "value": "testuser"},
                    "reason": "demonstrate fill",
                }
            )
    # No matching textbox — fall back to finish so the test doesn't hang.
    return json.dumps({"action": "finish", "args": {}})


async def test_llm_planner_unavailable_falls_back_to_deterministic(
    browser_manager: BrowserManager, llm_settings: Settings, local_site_url: str
) -> None:
    """When llm_provider=zai but the CLI is unavailable, the agent should
    still complete using the deterministic planner."""
    from sera_browser.task.agent import BrowserAgent, TaskConfig
    from sera_browser.task.llm_client import ZaiLLMClient

    # Construct a ZaiLLMClient pointed at a non-existent binary so
    # `available()` returns False.
    broken_client = ZaiLLMClient(cli_path="/nonexistent/z-ai-binary")

    agent = BrowserAgent(
        settings=llm_settings,
        browser_manager=browser_manager,
        llm_client=broken_client,
    )
    cfg = TaskConfig(
        task_id="test-llm-unavailable",
        target=local_site_url + "/",
        objective="Analyze the site.",
        browser_mode="headless",
        timeout_s=60,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost"],
        max_steps=5,
    )
    cancellation = CancellationManager(task_id="test-llm-unavailable")
    result = await agent.run(cfg, cancellation)
    assert result.report is not None
    # Should complete via deterministic fallback.
    assert result.report.status in {"completed", "blocked"}
