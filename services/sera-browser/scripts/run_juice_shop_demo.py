"""End-to-end Juice Shop demo script.

Usage:
    # Start Juice Shop first (e.g. cd /tmp/juice-shop && npm start)
    python scripts/run_juice_shop_demo.py --target http://127.0.0.1:3000

Saves the structured report to download/juice_shop_report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Ensure the package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sera_browser.browser.manager import BrowserManager
from sera_browser.config import Settings
from sera_browser.logging import configure_logging, get_logger
from sera_browser.models import TaskCreate
from sera_browser.session.manager import SessionManager

log = get_logger("sera.demo.juice_shop")


async def main(target: str, output: Path) -> int:
    configure_logging(level="INFO", fmt="text")
    settings = Settings(
        browser_mode="headless",
        local_lab=True,
        allowed_private_targets="127.0.0.1,localhost,127.0.0.1:3000",
        max_steps=20,
        max_task_duration_s=240,
        network_idle_timeout_ms=5000,
        navigation_timeout_ms=60000,
        log_level="INFO",
        log_format="text",
    )
    bm = BrowserManager(settings=settings)
    sm = SessionManager(settings=settings, browser_manager=bm)

    req = TaskCreate(
        target=target,
        task=(
            "Analyze this application for business flows, authentication, "
            "APIs, routes, technologies and security-relevant browser "
            "behavior."
        ),
        browser_mode="headless",
        timeout=240,
        local_lab=True,
        allowed_targets=["127.0.0.1", "localhost", "127.0.0.1:3000"],
        max_steps=20,
    )

    log.info("creating task against %s", target)
    started = time.perf_counter()
    rec = await sm.create(req)
    while rec.task is not None and not rec.task.done():
        await asyncio.sleep(0.5)
    elapsed = time.perf_counter() - started

    log.info("task finished: status=%s elapsed=%.2fs", rec.status.value, elapsed)

    if rec.report is None:
        log.error("no report produced: error=%s", rec.error)
        return 1

    report = rec.report
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("report saved to %s", output)

    # Print summary.
    print()
    print("=" * 70)
    print(f"  SERA Browser Agent — Juice Shop Demo Report")
    print("=" * 70)
    print(f"  target:       {report.target}")
    print(f"  title:        {report.title}")
    print(f"  status:       {report.status}")
    print(f"  duration_ms:  {report.duration_ms:.0f}")
    print(f"  steps:        {report.steps}")
    print(f"  browser_mode: {report.browser_mode}")
    print(f"  technology:   {', '.join(report.technology) or '(none)'}")
    print(f"  routes:       {len(report.routes)} route(s)")
    for r in report.routes[:8]:
        print(f"    - {r}")
    if len(report.routes) > 8:
        print(f"    ... and {len(report.routes) - 8} more")
    print(f"  apis:         {len(report.apis)} endpoint(s)")
    for a in report.apis[:8]:
        print(f"    - {a}")
    if len(report.apis) > 8:
        print(f"    ... and {len(report.apis) - 8} more")
    print(f"  forms:        {len(report.forms)} form(s)")
    print(f"  auth:         detected={report.authentication.detected} type={report.authentication.type}")
    print(f"  workflows:    {report.workflows}")
    print(f"  network:      {report.network.request_count} reqs / {report.network.response_count} resps")
    print(f"                graphql ops: {report.network.graphql_operations}")
    print(f"                websockets:   {report.network.websocket_endpoints}")
    print(f"  challenges:   {len(report.challenges)}")
    for c in report.challenges:
        print(f"    - {c.get('type')}: handled={c.get('handled')} ({c.get('handling')})")
    print(f"  screenshots:  {len(report.screenshots)}")
    print(f"  console errs: {len(report.console_errors)}")
    print(f"  summary:      {report.summary}")
    print("=" * 70)

    # Cleanup.
    await sm.shutdown_all()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="http://127.0.0.1:3000")
    parser.add_argument("--output", default="download/juice_shop_report.json")
    args = parser.parse_args()
    output_path = (Path(__file__).resolve().parents[1] / args.output).resolve()
    sys.exit(asyncio.run(main(args.target, output_path)))
