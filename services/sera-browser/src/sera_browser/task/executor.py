"""ActionExecutor — bounded, classified, evidence-producing action execution.

Rebuild v4 rules enforced here:

1. **The executor is authoritative.** LLM/vision output is untrusted input:
   refs must exist in the ElementRegistry, navigate must pass SSRF, and
   every action runs inside ONE bounded budget.
2. **One bounded action budget.** A failing click consumes at most
   ``action_timeout_ms`` plus ONE tightly-bounded retry
   (``action_retry_timeout_ms``) after a fresh re-resolution. There are no
   sequential fallback paths that each burn a full timeout.
3. **A failed action is never reissued indefinitely.** Failures feed the
   LoopRecoveryManager (failure-key tracking) and the ElementRegistry
   (failure counting → invalidation). Two failures of the same
   (action, ref) block that target for the rest of the task.
4. **Extraction data is preserved.** scrape/content results are returned
   as structured ``ActionResult.data``, not reduced to count-only strings.
5. **Network attribution.** The recorder watermark around each action
   attributes network requests to that action (Phase 8 correlation).

The executor does not decide what to do — it executes a planned step and
reports a classified outcome.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from playwright.async_api import Error as PlaywrightError, Page

from ..browser.driver import PlaywrightDriver
from ..browser.element_registry import ElementRegistry
from ..browser.inspector import Inspector
from ..cancel import CancellationManager
from ..config import Settings
from ..logging import get_logger
from ..models import BrowserState, InteractiveElement
from .failure import ActionFailure, classify_exception
from .loop import LoopRecoveryManager

log = get_logger("sera.task.executor")

# Hard caps for structured data carried in ActionResult (keeps memory +
# evidence bounded on pathological pages).
_MAX_SCRAPE_ITEMS = 60
_MAX_CONTENT_CHARS = 20_000


@dataclass
class ActionResult:
    action: str
    ok: bool
    duration_ms: float
    detail: str = ""
    screenshot: str | None = None
    # --- v4 fields -----------------------------------------------------
    failure: ActionFailure | None = None
    ref: str | None = None
    # Structured data (scrape/content/extract results) — preserved, not
    # reduced to counts.
    data: dict[str, Any] | None = None
    # Network requests attributed to this action (watermark delta).
    attributed_requests: list[dict[str, Any]] = field(default_factory=list)
    # URL before/after — navigation-change signal for the feedback channel.
    url_before: str | None = None
    url_after: str | None = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "ok": self.ok,
            "ms": round(self.duration_ms, 1),
        }
        if self.ref:
            out["ref"] = self.ref
        if self.failure is not None:
            out["failure"] = self.failure.as_dict()
        if self.detail:
            out["detail"] = self.detail[:300]
        return out


class ActionExecutor:
    def __init__(
        self,
        driver: PlaywrightDriver,
        inspector: Inspector,
        settings: Settings,
        cancellation: CancellationManager,
        *,
        tab_manager: Any = None,
        registry: ElementRegistry | None = None,
        recorder: Any = None,
    ) -> None:
        self.driver = driver
        self.inspector = inspector
        self.settings = settings
        self.cancellation = cancellation
        self._tab_manager = tab_manager
        self._recorder = recorder
        # The authoritative registry: falls back to the inspector's.
        self.registry = registry if registry is not None else inspector.registry
        # Optional structured-extraction handler (wired by the agent when
        # an LLM client is available). Signature:
        #   async (page, prompt, schema) -> dict
        self._extraction_handler: Callable[..., Awaitable[dict]] | None = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def execute(
        self,
        page: Page,
        state: BrowserState,
        loop: LoopRecoveryManager,
        action: str,
        **kwargs: Any,
    ) -> ActionResult:
        started = time.perf_counter()
        url_before = None
        try:
            url_before = page.url
        except PlaywrightError:
            url_before = None
        watermark = self._request_watermark()

        # Include the most meaningful arg in the loop-detection key so that
        # `navigate(A)` and `navigate(B)` are NOT considered the same call.
        loop_key_args = (
            kwargs.get("url")
            or kwargs.get("ref")
            or kwargs.get("selector")
            or kwargs.get("key")
            or ""
        )
        loop.record_tool(action, str(loop_key_args))

        ok = False
        detail = ""
        failure: ActionFailure | None = None
        data: dict[str, Any] | None = None
        ref = kwargs.get("ref")

        try:
            if action == "navigate":
                url = kwargs["url"]
                force = kwargs.get("force", False)
                timing = await self.driver.navigate(page, url, force=force)
                ok = True
                detail = f"nav ms={timing.total_ms}"
            elif action == "back":
                ok = await self.driver.back(page)
            elif action == "forward":
                ok = await self.driver.forward(page)
            elif action == "reload":
                timing = await self.driver.reload(page)
                ok = True
                detail = f"reload ms={timing.total_ms}"
            elif action == "click":
                ok, failure, detail = await self._act_on_ref(
                    page, ref, lambda loc: self.driver.click_locator(page, loc)
                )
            elif action == "fill":
                value = kwargs["value"]
                ok, failure, detail = await self._act_on_ref(
                    page, ref, lambda loc: self.driver.fill_locator(page, loc, value)
                )
            elif action == "select":
                value = kwargs["value"]
                ok, failure, detail = await self._act_on_ref(
                    page, ref, lambda loc: self.driver.select_locator(page, loc, value)
                )
            elif action == "check" or action == "uncheck":
                target = action == "check"
                ok, failure, detail = await self._act_on_ref(
                    page, ref, lambda loc: self.driver.check_locator(page, loc, target)
                )
            elif action == "hover":
                ok, failure, detail = await self._act_on_ref(
                    page, ref, lambda loc: self.driver.hover_locator(page, loc)
                )
            elif action == "scroll":
                ok = await self.driver.scroll(
                    page, selector=kwargs.get("selector"), dy=kwargs.get("dy", 400)
                )
            elif action == "press":
                ok = await self.driver.keyboard_press(page, kwargs["key"])
            elif action == "mouse_click":
                ok = await self.driver.mouse_click(page, kwargs["x"], kwargs["y"])
            elif action == "fill_form":
                fields = kwargs["fields"]
                filled, failed = await self._fill_form(page, fields)
                ok = not failed
                detail = f"filled={filled} failed={len(failed)}"
                if failed:
                    detail += f" failed_refs={failed[:3]}"
                    failure = ActionFailure(
                        kind="not_found", detail=f"fields failed: {failed[:5]}"
                    )
            elif action in ("switch_tab", "close_tab", "new_tab"):
                tm = self._tab_manager
                if tm is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="tab management not enabled")
                    detail = "tab management not enabled"
                elif action == "switch_tab":
                    ok = tm.switch_to(kwargs["tab_id"])
                    if ok:
                        detail = f"switched to {kwargs['tab_id']}"
                elif action == "close_tab":
                    ok = tm.close_tab(kwargs["tab_id"])
                    detail = f"closed {kwargs['tab_id']}"
                elif action == "new_tab":
                    rec = await tm.new_tab_async(url=kwargs.get("url"))
                    ok = True
                    detail = f"opened {rec.id}"
            elif action == "extract":
                ok, failure, detail, data = await self._extract(page, kwargs)
            elif action == "scrape":
                item_sel = kwargs["item"]
                fields = kwargs["fields"]
                try:
                    result = await self._scrape(page, item_sel, fields)
                    ok = True
                    # v4: preserve the items, not just the count.
                    data = {
                        "count": result["count"],
                        "items": result["items"][:_MAX_SCRAPE_ITEMS],
                    }
                    detail = f"scraped {result['count']} items (data preserved)"
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"scrape failed: {e!r}"
            elif action == "content":
                fmt = kwargs.get("format", "txt")
                selector = kwargs.get("selector")
                max_chars = kwargs.get("max_chars", 50000)
                try:
                    content = await self._fetch_content(page, fmt, selector, max_chars)
                    ok = True
                    data = {
                        "format": fmt,
                        "chars": len(content),
                        # Preserve a bounded slice of real content for the
                        # planner + evidence (not just the length).
                        "preview": content[:_MAX_CONTENT_CHARS],
                    }
                    detail = f"content {fmt} {len(content)} chars (data preserved)"
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"content failed: {e!r}"
            elif action == "search_page":
                pattern = kwargs["pattern"]
                try:
                    import re

                    content = await page.inner_text("body")
                    matches = re.findall(pattern, content)
                    ok = True
                    data = {
                        "count": len(matches),
                        "matches": [str(m)[:100] for m in matches[:50]],
                    }
                    detail = f"found {len(matches)} matches"
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"search failed: {e!r}"
            elif action == "history":
                hm = getattr(self, "_history_manager", None)
                if hm is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="history not enabled")
                    detail = "history not enabled"
                else:
                    entries = hm.list_entries()
                    ok = True
                    data = {"count": len(entries)}
                    detail = f"{len(entries)} entries"
            elif action == "replay":
                hm = getattr(self, "_history_manager", None)
                if hm is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="history not enabled")
                    detail = "history not enabled"
                else:
                    seq = kwargs["seq"]
                    try:
                        await hm.replay(seq, page, self)
                        ok = True
                        detail = f"replayed seq={seq}"
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        failure = classify_exception(e)
                        detail = f"replay failed: {e!r}"
            elif action == "restrict":
                sg = getattr(self, "_soft_restrict", None)
                if sg is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="soft restrict not enabled")
                    detail = "soft restrict not enabled"
                else:
                    allowed = kwargs.get("allowed", [])
                    sg.set_restriction(allowed)
                    ok = True
                    detail = f"restricted to {len(allowed)} tools"
            elif action == "flush_auth":
                sm = getattr(self, "_storage_manager", None)
                if sm is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="storage not enabled")
                    detail = "storage not enabled"
                else:
                    try:
                        cookies = await sm.get_cookies(page)
                        ok = True
                        data = {"cookies": len(cookies)}
                        detail = f"exported {len(cookies)} cookies"
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        failure = classify_exception(e)
                        detail = f"flush_auth failed: {e!r}"
            elif action == "restore_auth":
                sm = getattr(self, "_storage_manager", None)
                if sm is None:
                    ok = False
                    failure = ActionFailure(kind="exception", detail="storage not enabled")
                    detail = "storage not enabled"
                else:
                    cookies = kwargs.get("cookies", [])
                    try:
                        await sm.add_cookies(page, cookies)
                        ok = True
                        detail = f"restored {len(cookies)} cookies"
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        failure = classify_exception(e)
                        detail = f"restore_auth failed: {e!r}"
            elif action == "find_elements":
                selector = kwargs.get("selector", "")
                try:
                    if selector:
                        count = await page.locator(selector).count()
                    else:
                        count = 0
                    ok = True
                    data = {"count": count}
                    detail = f"found {count} elements"
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"find failed: {e!r}"
            elif action == "drag":
                start_ref = kwargs["start_ref"]
                end_ref = kwargs["end_ref"]
                start_loc = await self.registry.resolve(page, start_ref)
                end_loc = await self.registry.resolve(page, end_ref)
                if start_loc is None or end_loc is None:
                    ok = False
                    failure = ActionFailure(kind="not_found", detail="drag refs not found")
                    detail = "drag refs not found"
                else:
                    try:
                        await self.driver.hover_locator(page, start_loc)
                        await page.mouse.down()
                        await self.driver.hover_locator(page, end_loc)
                        await page.mouse.up()
                        ok = True
                        detail = f"drag {start_ref} -> {end_ref}"
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        failure = classify_exception(e)
                        detail = f"drag failed: {e!r}"
            elif action == "save_pdf":
                from pathlib import Path

                pdf_path = Path(
                    kwargs.get("path")
                    or str(self.settings.session_root / state.task_id / "page.pdf")
                )
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await page.pdf(path=str(pdf_path))
                    ok = True
                    detail = str(pdf_path)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"pdf failed: {e!r}"
            elif action == "upload_file":
                file_path = kwargs["path"]
                loc = await self.registry.resolve(page, ref)
                if loc is None:
                    ok = False
                    failure = ActionFailure(kind="not_found", detail=f"upload ref not found: {ref}")
                    detail = f"upload ref not found: {ref}"
                else:
                    ok = await self.driver.upload_locator(page, loc, file_path)
                    if not ok:
                        failure = ActionFailure(kind="timeout", detail="upload not actionable")
                    detail = f"uploaded {file_path}" if ok else "upload failed"
            elif action == "update_memory":
                ok = True
                detail = f"memory updated ({len(kwargs['memory'])} chars)"
            elif action == "evaluate":
                try:
                    result = await self.driver.eval_js(page, kwargs["script"])
                    ok = True
                    detail = f"eval result type={type(result).__name__}"
                except PermissionError as e:
                    ok = False
                    failure = ActionFailure(kind="nav_rejected", detail=f"eval blocked: {e}")
                    detail = f"eval blocked: {e}"
                except Exception as e:  # noqa: BLE001
                    ok = False
                    failure = classify_exception(e)
                    detail = f"eval failed: {e!r}"
            elif action == "screenshot":
                from ..browser.screenshots import ScreenshotLayer

                layer = ScreenshotLayer(
                    self.settings.session_root / state.task_id, self.cancellation
                )
                path = await layer.capture(page, label=kwargs.get("label", "step"))
                ok = path is not None
                if ok and path:
                    state.screenshots.append(str(path))
                    detail = str(path)
            elif action == "noop":
                ok = True
            else:
                ok = False
                failure = ActionFailure(kind="exception", detail=f"unknown action: {action}")
                detail = f"unknown action: {action}"
        except Exception as e:  # noqa: BLE001
            ok = False
            failure = classify_exception(e)
            detail = f"exc: {e!r}"

        dur = (time.perf_counter() - started) * 1000.0
        state.last_action = action
        state.timings.last_action_ms = dur
        state.timings.total_steps += 1
        if ok:
            loop.reset_step()
            if ref:
                self.registry.mark_interacted(ref)
        else:
            # Failure feedback: registry failure counting (→ invalidation)
            # and loop-manager failure keys (→ target blocking).
            if ref:
                self.registry.mark_failed(ref, reason=failure.kind if failure else "")
            loop.record_failure(action, str(loop_key_args), failure.kind if failure else "")

        url_after = None
        try:
            url_after = page.url
        except PlaywrightError:
            url_after = None
        attributed = self._attributed_requests(watermark)

        log.info(
            "action",
            extra={
                "event": "action",
                "action": action,
                "ok": ok,
                "ms": round(dur, 1),
                "detail": detail[:200],
                "failure": failure.kind if failure else None,
                "ref": ref,
                "attributed_requests": len(attributed),
            },
        )
        return ActionResult(
            action=action,
            ok=ok,
            duration_ms=dur,
            detail=detail,
            failure=failure,
            ref=ref,
            data=data,
            attributed_requests=attributed,
            url_before=url_before,
            url_after=url_after,
        )

    # ------------------------------------------------------------------
    # Ref-action core: resolve → ONE budget → classified outcome
    # ------------------------------------------------------------------

    async def _act_on_ref(
        self,
        page: Page,
        ref: str | None,
        act: Callable[[Any], Awaitable[bool]],
    ) -> tuple[bool, ActionFailure | None, str]:
        """Execute a locator action on a registry ref.

        Protocol:
          1. Registry lookup — unknown/invalidated refs fail immediately
             (no browser roundtrip, no budget burn).
          2. Fresh resolution (fast count checks, no auto-wait).
          3. ONE action budget.
          4. On a retryable failure: ONE re-resolution + ONE short retry
             budget. Never more.
        """
        if not ref:
            return False, ActionFailure(kind="unknown_ref", detail="missing ref"), "missing ref"
        rec = self.registry.get(ref)
        if rec is None:
            return (
                False,
                ActionFailure(kind="stale_ref", detail=f"ref {ref} not in current registry"),
                f"ref not in current registry: {ref}",
            )
        if rec.element.disabled:
            return (
                False,
                ActionFailure(kind="not_actionable", detail=f"element {ref} disabled"),
                f"element {ref} disabled",
            )

        # Attempt 1: fresh resolution + full budget.
        loc = await self.registry.resolve(page, ref)
        if loc is None:
            # Zero strategies matched. Retryable: the DOM may be mid-update.
            failure = ActionFailure(kind="not_found", detail=f"no locator matched ref {ref}")
            return False, failure, f"element not found on page: {ref}"
        ok = await act(loc)
        if ok:
            return True, None, "ok"

        # Attempt 2 (the ONLY retry): re-resolve + short budget. The act
        # closure already carries the budget settings; re-resolution picks
        # up elements that appeared/settled since attempt 1.
        retry_budget_ms = getattr(self.settings, "action_retry_timeout_ms", 3000)
        old_budget = self.settings.action_timeout_ms
        try:
            self.settings.action_timeout_ms = retry_budget_ms
            loc2 = await self.registry.resolve(page, ref)
            if loc2 is None:
                return (
                    False,
                    ActionFailure(kind="not_found", detail=f"ref {ref} vanished before retry"),
                    f"element not found on retry: {ref}",
                )
            ok2 = await act(loc2)
            if ok2:
                return (
                    True,
                    None,
                    f"ok on retry (budget {retry_budget_ms}ms)",
                )
        finally:
            self.settings.action_timeout_ms = old_budget
        return (
            False,
            ActionFailure(kind="timeout", detail=f"ref {ref} not actionable within budget"),
            f"action failed on ref {ref} (budget {old_budget}ms + retry {retry_budget_ms}ms)",
        )

    async def _fill_form(
        self, page: Page, fields: list[dict[str, Any]]
    ) -> tuple[int, list[str]]:
        """Bulk fill. Returns (filled_count, failed_refs)."""
        filled = 0
        failed: list[str] = []
        for f in fields:
            ref = f["ref"]
            value = f["value"]
            ok, _failure, _detail = await self._act_on_ref(
                page, ref, lambda loc: self.driver.fill_locator(page, loc, value)
            )
            if ok:
                filled += 1
            else:
                failed.append(ref)
        return filled, failed

    async def _extract(
        self, page: Page, kwargs: dict[str, Any]
    ) -> tuple[bool, ActionFailure | None, str, dict[str, Any] | None]:
        """Structured extraction via the wired handler (LLM-backed).

        Without a handler this is a CLASSIFIED failure, never a fake
        success (the old executor returned ok=True with the prompt as
        detail — a lie that broke the feedback channel).
        """
        handler = self._extraction_handler
        if handler is None:
            return (
                False,
                ActionFailure(kind="provider_error", detail="no extraction handler wired"),
                "extract requires an extraction handler (LLM) — not wired",
                None,
            )
        try:
            result = await handler(
                page=page, prompt=kwargs["prompt"], schema=kwargs.get("schema")
            )
            return True, None, f"extracted ({len(result)} keys)", result
        except Exception as e:  # noqa: BLE001
            return (
                False,
                classify_exception(e),
                f"extract failed: {e!r}",
                None,
            )

    # ------------------------------------------------------------------
    # Network attribution (Phase 8 correlation)
    # ------------------------------------------------------------------

    def _request_watermark(self) -> int:
        if self._recorder is None:
            return 0
        try:
            return len(self._recorder.requests)
        except Exception:  # noqa: BLE001
            return 0

    def _attributed_requests(self, watermark: int) -> list[dict[str, Any]]:
        """Requests recorded between the watermark and now, attributed to
        the action that just executed. Compact form for evidence/graph."""
        if self._recorder is None:
            return []
        try:
            reqs = list(self._recorder.requests)[watermark:]
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for r in reqs:
            if r.resource_type in ("document", "xhr", "fetch", "websocket") or r.graphql:
                out.append(
                    {
                        "method": r.method,
                        "url": r.url,
                        "status": r.status,
                        "resource_type": r.resource_type,
                        "graphql": r.graphql,
                    }
                )
        return out[:40]

    # ------------------------------------------------------------------
    # dsh-browser-inspired helpers
    # ------------------------------------------------------------------

    async def _scrape(
        self,
        page: Page,
        item_sel: str,
        fields: list[dict[str, str]],
    ) -> dict[str, Any]:
        """CSS-based structured extraction (dsh-browser `browser_scrape`).

        Static CSS only — no arbitrary JS — so it's CSP-safe. Supports
        `selector@attr` syntax (e.g. `a@href` returns the absolute URL).
        """
        specs = [{"name": f["name"], "selector": f["selector"]} for f in fields]
        script = f"""(() => {{
            const itemSel = {json.dumps(item_sel)};
            const fields = {json.dumps(specs)};
            const parseField = f => {{
                const m = /^(.*)@([A-Za-z][A-Za-z0-9_-]*)$/.exec(f.selector.trim());
                if (m) return {{ base: m[1].trim(), attr: m[2] }};
                return {{ base: f.selector, attr: null }};
            }};
            let items = [];
            try {{ items = Array.from(document.querySelectorAll(itemSel)); }}
            catch (e) {{ return {{ ok: false, error: String(e) }}; }}
            if (items.length === 0) return {{ ok: false, error: 'no elements matched: ' + itemSel }};
            const out = [];
            for (const it of items) {{
                const row = {{}};
                for (const f of fields) {{
                    const {{ base, attr }} = parseField(f);
                    let el = null;
                    try {{ el = base === '' ? it : it.querySelector(base); }} catch {{ el = null; }}
                    if (!el) {{ row[f.name] = null; continue; }}
                    if (attr !== null) {{
                        const v = el.getAttribute(attr);
                        row[f.name] = attr === 'href' && el.tagName === 'A' && v
                            ? new URL(v, location.href).href : v;
                        continue;
                    }}
                    row[f.name] = (el.textContent || '').trim();
                }}
                out.push(row);
            }}
            return {{ ok: true, count: out.length, items: out }};
        }})()"""
        result = await page.evaluate(script)
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error", "unknown") if isinstance(result, dict) else "invalid response"
            raise RuntimeError(error)
        return {"count": result["count"], "items": result["items"]}

    async def _fetch_content(
        self,
        page: Page,
        fmt: str,
        selector: str | None,
        max_chars: int,
    ) -> str:
        """Multi-format page content fetch (dsh-browser `browser_content`).

        Formats: html | markdown | txt | json. Optional CSS selector scoping.
        """
        if fmt == "html":
            if selector:
                content = await page.inner_html(selector)
            else:
                content = await page.content()
        elif fmt == "txt":
            if selector:
                content = await page.inner_text(selector)
            else:
                content = await page.inner_text("body")
        elif fmt == "markdown":
            if selector:
                html = await page.inner_html(selector)
            else:
                html = await page.content()
            content = self._html_to_markdown(html)
        elif fmt == "json":
            content = await page.evaluate("""() => {
                const scripts = document.querySelectorAll('script[type="application/json"]');
                const out = [];
                for (const s of scripts) {
                    try { out.push(JSON.parse(s.textContent)); } catch {}
                }
                return JSON.stringify(out, null, 2);
            }""")
        else:
            raise ValueError(f"unknown format: {fmt}")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content

    @staticmethod
    def _html_to_markdown(html: str) -> str:
        """Very lightweight HTML→markdown conversion."""
        import re

        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        for i in range(6, 0, -1):
            html = re.sub(
                rf"<h{i}[^>]*>(.*?)</h{i}>", rf"{'#' * i} \1\n", html, flags=re.IGNORECASE | re.DOTALL
            )
        html = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(
            r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", html, flags=re.IGNORECASE | re.DOTALL
        )
        html = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"</?(p|div|br)[^>]*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", "", html)
        html = re.sub(r"\n{3,}", "\n\n", html)
        return html.strip()
