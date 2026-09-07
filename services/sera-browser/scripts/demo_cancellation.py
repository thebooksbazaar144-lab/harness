"""Cancellation demo script.

Starts a long browser task, cancels it after 3 seconds, then verifies no
orphan chromium processes remain.

Usage:
    python scripts/demo_cancellation.py --target http://127.0.0.1:3000
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sera_browser.browser.manager import BrowserManager
from sera_browser.config import Settings
from sera_browser.logging import configure_logging, get_logger
from sera_browser.models import TaskCreate
from sera_browser.session.manager import SessionManager

log = get_logger("sera.demo.cancel")


def count_chromium() -> int:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "ms-playwright/chromium"], text=True, stderr=subprocess.DEVNULL
        )
        return len([p for p in out.split() if p.strip().isdigit()])
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return 0


async def main(target: str) -> int:
    configure_logging(level="INFO", fmt="text")
    settings = Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost,127.0.0.1:3000",
        max_steps=100,  # high so the task is still running when we cancel
        max_task_duration_s=300,
        network_idle_timeout_ms=2000,
        log_level="INFO",
        log_format="text",
    )
    bm = BrowserManager(settings=settings)
    sm = SessionManager(settings=settings, browser_manager=bm)

    req = TaskCreate(
        target=target,
        task="Long-running analysis task that should be cancelled mid-flight.",
        browser_mode="headless",
        timeout=300,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost", "127.0.0.1:3000"],
        max_steps=100,
    )

    print()
    print("=" * 70)
    print("  SERA Browser Agent — Cancellation Demo")
    print("=" * 70)

    rec = await sm.create(req)
    print(f"  [1] task created:        {rec.task_id} status={rec.status.value}")

    # Count chromium processes while task is running.
    await asyncio.sleep(1.0)
    active = count_chromium()
    print(f"  [2] browser active:       {active} chromium process(es) running")

    # Cancel after 3 seconds.
    await asyncio.sleep(2.0)
    print(f"  [3] cancelling task:      {rec.task_id}")
    cancel_started = time.perf_counter()
    ok = await sm.cancel(rec.task_id, reason="demo-cancel")
    cancel_elapsed = time.perf_counter() - cancel_started
    print(f"  [4] cancel returned:      ok={ok} elapsed={cancel_elapsed*1000:.0f}ms")

    # Give the cleanup a moment.
    await asyncio.sleep(1.0)
    after = count_chromium()
    print(f"  [5] after cancellation:   {after} chromium process(es) remaining")

    # Force-cleanup (final safety net).
    await sm.shutdown_all()
    await asyncio.sleep(1.0)
    final = count_chromium()
    print(f"  [6] after force-cleanup:  {final} chromium process(es) remaining")

    print()
    print(f"  task final status: {rec.status.value}")
    if rec.report:
        print(f"  report status:     {rec.report.status}")
        print(f"  report error:      {rec.report.error or '(none)'}")
    print()
    print("  RESULT: " + ("PASS — no leaked browser processes" if final == 0 else "FAIL — leaked processes"))
    print("=" * 70)
    return 0 if final == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.target)))
