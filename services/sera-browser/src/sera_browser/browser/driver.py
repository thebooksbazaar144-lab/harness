"""PlaywrightDriver — direct async Playwright primitives.

This is the normal execution path. Every method is bounded by a timeout and
respects the cooperative cancellation token.

The accessibility snapshot/ref-map format is Playwright-MCP-inspired but
implemented directly. We do not depend on Playwright MCP at runtime.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Iterable

from playwright.async_api import (
    Browser,
    BrowserContext,
    BrowserType,
    Dialog,
    Download,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ..cancel import CancellationManager, TaskCancelledError
from ..config import Settings
from ..logging import get_logger
from ..models import (
    AccessibilityNode,
    ConsoleMessage,
    DownloadRecord,
    InteractiveElement,
    NetworkRequest,
    PageError,
    WebSocketEvent,
)

log = get_logger("sera.browser.driver")


@dataclass
class NavigationTiming:
    started: float = 0.0
    dom_content_loaded: float | None = None
    load: float | None = None
    network_idle: float | None = None
    finished: float | None = None

    @property
    def total_ms(self) -> float | None:
        if self.finished and self.started:
            return (self.finished - self.started) * 1000.0
        return None


@dataclass
class SnapshotResult:
    """Result of an accessibility/DOM snapshot."""

    tree: list[AccessibilityNode]
    elements: list[InteractiveElement]
    raw: str
    dom_hash: str
    url: str
    title: str


@dataclass
class _DialogState:
    """Tracks dialog auto-handlers so we don't register them per-page forever."""

    page: Page
    registered: bool = False


class PlaywrightDriver:
    """Direct Playwright primitive layer.

    Owns no Playwright lifecycle itself — `BrowserManager` launches the
    browser/context and hands `Page` objects to the driver. This separation
    keeps the driver testable and lets the fallback borrow the same page.
    """

    def __init__(
        self,
        settings: Settings,
        cancellation: CancellationManager,
    ) -> None:
        self.settings = settings
        self.cancellation = cancellation
        self._dialog_states: dict[int, _DialogState] = {}

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        page: Page,
        url: str,
        *,
        wait_until: str = "load",
        force: bool = False,
        network_idle_timeout: float | None = None,
    ) -> NavigationTiming:
        """Idempotent navigate.

        If `url == page.url` and `force` is False, only refresh the snapshot
        (no reload). Otherwise navigate, then optionally wait for network-idle
        bounded by `network_idle_timeout`.
        """
        self.cancellation.check()
        timing = NavigationTiming(started=time.perf_counter())

        current = page.url
        if current and current.rstrip("/") == url.rstrip("/") and not force:
            # Idempotent: don't reload. Still record timing as ~0.
            timing.dom_content_loaded = timing.started
            timing.load = timing.started
            timing.finished = timing.started
            log.info(
                "navigate skipped (same url)",
                extra={"event": "navigate.skip", "url": url, "task_id": self.cancellation.task_id},
            )
            return timing

        log.info(
            "navigate",
            extra={"event": "navigate", "url": url, "task_id": self.cancellation.task_id},
        )

        # Bounded navigation timeout
        nav_timeout = self.settings.navigation_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                page.goto(url, wait_until=wait_until, timeout=nav_timeout),
                timeout=nav_timeout / 1000.0 + 5.0,
            )
        except TaskCancelledError:
            raise
        except PlaywrightTimeoutError:
            # Navigation timed out, but page may have partially loaded.
            log.warning(
                "navigate timeout (continuing with partial state)",
                extra={"event": "navigate.timeout", "url": url},
            )

        timing.dom_content_loaded = time.perf_counter()

        # Optional bounded network-idle wait (NEVER wait indefinitely).
        if network_idle_timeout is None:
            network_idle_timeout = self.settings.network_idle_timeout_ms / 1000.0
        if network_idle_timeout > 0:
            try:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"),
                    timeout=network_idle_timeout,
                )
                timing.network_idle = time.perf_counter()
            except (PlaywrightTimeoutError, asyncio.TimeoutError):
                # network-idle is best-effort for SPAs
                pass

        timing.load = time.perf_counter()
        timing.finished = time.perf_counter()
        return timing

    async def back(self, page: Page) -> bool:
        self.cancellation.check()
        try:
            await self.cancellation.wait_or_cancel(page.go_back(), timeout=15.0)
            return True
        except PlaywrightError as e:
            log.warning("back failed", extra={"event": "back.fail", "err": str(e)})
            return False

    async def forward(self, page: Page) -> bool:
        self.cancellation.check()
        try:
            await self.cancellation.wait_or_cancel(page.go_forward(), timeout=15.0)
            return True
        except PlaywrightError as e:
            log.warning("forward failed", extra={"event": "forward.fail", "err": str(e)})
            return False

    async def reload(self, page: Page, *, wait_until: str = "load") -> NavigationTiming:
        self.cancellation.check()
        timing = NavigationTiming(started=time.perf_counter())
        try:
            await self.cancellation.wait_or_cancel(
                page.reload(wait_until=wait_until, timeout=self.settings.navigation_timeout_ms),
                timeout=self.settings.navigation_timeout_ms / 1000.0 + 5.0,
            )
        except PlaywrightTimeoutError:
            log.warning("reload timeout", extra={"event": "reload.timeout"})
        timing.finished = time.perf_counter()
        return timing

    # ------------------------------------------------------------------
    # Snapshots / DOM
    # ------------------------------------------------------------------

    async def snapshot(self, page: Page, registry: Any = None) -> SnapshotResult:
        """Capture accessibility tree + interactive elements.

        Refs are assigned by the ElementRegistry (single authoritative
        namespace). Pass ``registry=`` to get registry-assigned refs; when
        omitted (legacy/tests), elements carry ``ref=None``.

        Strategy (SOTA, browser-use / Playwright MCP inspired):
        1. Try CDP ``Accessibility.getFullAXTree`` first — richer ARIA
           semantics, pierces shadow DOM.
        2. Fall back to DOM-based extraction if CDP is unavailable
           (non-Chromium browser), the page isn't loaded, or the AX tree
           is empty (rare; canvas-only pages).
        """
        self.cancellation.check()
        started = time.perf_counter()

        # Try CDP-based AX tree first (only if snapshot_method is cdp or auto).
        raw_nodes: list[dict[str, Any]] = []
        used_cdp = False
        snapshot_method = getattr(self.settings, "snapshot_method", "dom")
        if snapshot_method in ("cdp", "auto"):
            try:
                from .cdp_ax import CDPAccessibilityExtractor
                extractor = CDPAccessibilityExtractor(page)
                cdp_tree = await self.cancellation.wait_or_cancel(extractor.extract(), timeout=10.0)
                if cdp_tree:
                    raw_nodes = self._cdp_to_dom_format(cdp_tree)
                    used_cdp = True
                    log.debug(
                        "snapshot via CDP AX tree",
                        extra={"event": "snapshot.cdp", "nodes": len(raw_nodes)},
                    )
            except Exception as e:  # noqa: BLE001
                log.debug(
                    "CDP AX extraction failed, falling back to DOM",
                    extra={"event": "snapshot.cdp.fallback", "err": str(e)},
                )

        # Fall back to DOM-based extraction.
        if not raw_nodes:
            raw_nodes = await self._extract_dom_tree(page)

        elements: list[InteractiveElement] = []
        tree = self._build_tree(raw_nodes, elements)

        # Registry assigns the authoritative refs (stable identity across
        # snapshots; tombstones for disappearing elements).
        if registry is not None:
            reg_snap = registry.observe(elements)
            elements = reg_snap.elements

        # Build a stable raw representation and hash it for loop detection.
        # Refs are excluded from the hash: identity churn in ref numbering
        # must not look like DOM change.
        raw = self._serialize_tree(tree)
        import hashlib

        dom_hash = hashlib.sha1(raw.encode("utf-8")).hexdigest()  # nosec: B324

        url = page.url
        title = await self._safe_title(page)

        res = SnapshotResult(tree=tree, elements=elements, raw=raw, dom_hash=dom_hash, url=url, title=title)
        dur = (time.perf_counter() - started) * 1000.0
        log.debug(
            "snapshot done",
            extra={
                "event": "snapshot.done",
                "method": "cdp" if used_cdp else "dom",
                "elements": len(elements),
                "ms": round(dur, 1),
                "dom_hash": dom_hash[:10],
            },
        )
        return res

    @staticmethod
    def _cdp_to_dom_format(cdp_tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert CDP AX nodes to the DOM-walker format expected by _build_tree.

        The DOM walker produces nodes with: role, name, tag, href, type,
        value, placeholder, checked, disabled, hidden, children.
        CDP nodes have: role, name, backendNodeId, properties, htmlAttributes,
        interactive, children.
        """
        from .cdp_ax import cdp_nodes_to_elements

        def convert(node: dict[str, Any]) -> dict[str, Any]:
            props = node.get("properties", {})
            attrs = node.get("htmlAttributes", {})
            return {
                "role": node.get("role", ""),
                "name": node.get("name", ""),
                "tag": attrs.get("tag") or attrs.get("tagName"),
                "href": attrs.get("href"),
                "type": attrs.get("type"),
                "value": attrs.get("value"),
                "placeholder": attrs.get("placeholder"),
                "checked": props.get("checked"),
                "disabled": bool(props.get("disabled", False)),
                "hidden": bool(props.get("hidden", False)),
                "children": [convert(c) for c in node.get("children", [])],
            }
        return [convert(n) for n in cdp_tree]

    async def _extract_dom_tree(self, page: Page) -> list[dict[str, Any]]:
        """Extract a synthetic accessibility-like tree from the DOM.

        Returns a list of root nodes; each node has role, name, href, tag,
        type, value, placeholder, checked, disabled, and children.
        """
        try:
            data = await self.cancellation.wait_or_cancel(
                page.evaluate(
                    """() => {
                        const INTERACTIVE = new Set([
                            'A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY',
                        ]);
                        const ROLE_ATTR = ['role','aria-role'];
                        const NAME_ATTRS = ['aria-label','title','placeholder','name','alt'];

                        function getRole(el) {
                            const explicit = el.getAttribute('role') || el.getAttribute('aria-role');
                            if (explicit) return explicit.toLowerCase();
                            const tag = el.tagName;
                            switch (tag) {
                                case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
                                case 'BUTTON': return 'button';
                                case 'INPUT':
                                    switch ((el.getAttribute('type') || 'text').toLowerCase()) {
                                        case 'checkbox': return 'checkbox';
                                        case 'radio': return 'radio';
                                        case 'submit': return 'button';
                                        case 'button': return 'button';
                                        case 'password': return 'textbox';
                                        case 'email': return 'textbox';
                                        case 'search': return 'searchbox';
                                        case 'number': return 'spinbutton';
                                        case 'range': return 'slider';
                                        case 'color': return 'textbox';
                                        case 'date':
                                        case 'datetime-local':
                                        case 'time':
                                            return 'textbox';
                                        default: return 'textbox';
                                    }
                                case 'SELECT': return 'combobox';
                                case 'TEXTAREA': return 'textbox';
                                case 'SUMMARY': return 'button';
                                case 'OPTION': return 'option';
                            }
                            return null;
                        }

                        function getName(el) {
                            const aria = el.getAttribute('aria-label');
                            if (aria) return aria.trim();
                            const title = el.getAttribute('title');
                            if (title) return title.trim();
                            const placeholder = el.getAttribute('placeholder');
                            if (placeholder) return placeholder.trim();
                            const alt = el.getAttribute('alt');
                            if (alt) return alt.trim();
                            const labelFor = el.getAttribute('id');
                            if (labelFor) {
                                const lbl = document.querySelector('label[for="' + CSS.escape(labelFor) + '"]');
                                if (lbl && lbl.textContent) return lbl.textContent.trim();
                            }
                            // textContent for buttons/links
                            const txt = (el.textContent || '').trim();
                            if (txt && txt.length < 200) return txt;
                            const name = el.getAttribute('name');
                            if (name) return name;
                            return '';
                        }

                        function walk(el) {
                            const role = getRole(el);
                            // Only include elements with a role OR explicit interactive tag
                            const tag = el.tagName;
                            const isInteractive = INTERACTIVE.has(tag) || el.hasAttribute('onclick') || role;
                            if (!isInteractive && !role) {
                                // Recurse into children to find interactive descendants.
                                const out = [];
                                for (const c of el.children) {
                                    const sub = walk(c);
                                    if (Array.isArray(sub)) out.push(...sub);
                                    else if (sub) out.push(sub);
                                }
                                return out.length ? out : null;
                            }
                            const node = {
                                role: role || tag.toLowerCase(),
                                name: getName(el),
                                tag: tag.toLowerCase(),
                                href: el.getAttribute('href') || null,
                                type: el.getAttribute('type') || null,
                                value: el.value !== undefined && el.value !== '' ? String(el.value).slice(0, 200) : null,
                                placeholder: el.getAttribute('placeholder') || null,
                                checked: el.checked !== undefined ? !!el.checked : null,
                                disabled: !!el.disabled,
                                hidden: el.offsetParent === null && el.getClientRects().length === 0,
                            };
                            const children = [];
                            for (const c of el.children) {
                                const sub = walk(c);
                                if (Array.isArray(sub)) children.push(...sub);
                                else if (sub) children.push(sub);
                            }
                            if (children.length) node.children = children;
                            return node;
                        }

                        // Walk top-level body children; collect roots.
                        const roots = [];
                        const body = document.body;
                        if (!body) return roots;
                        for (const c of body.children) {
                            const sub = walk(c);
                            if (Array.isArray(sub)) roots.push(...sub);
                            else if (sub) roots.push(sub);
                        }
                        return roots;
                    }"""
                ),
                timeout=15.0,
            )
            return data or []
        except PlaywrightError as e:
            log.warning("dom tree extract failed", extra={"event": "snapshot.dom.fail", "err": str(e)})
            return []

    def _build_tree(
        self,
        nodes: list[dict[str, Any]],
        elements: list[InteractiveElement],
    ) -> list[AccessibilityNode]:
        """Build the accessibility tree + flat interactive-element list.

        NOTE (rebuild v4): this method NO LONGER assigns refs. Refs are the
        exclusive domain of the ElementRegistry — the single authoritative
        reference namespace. Elements arrive here with ``ref=None`` and get
        refs assigned at the Inspector/registry layer.
        """
        out: list[AccessibilityNode] = []
        for node in nodes:
            role = str(node.get("role") or "")
            name = str(node.get("name") or "").strip()
            children_in = node.get("children") or []
            children_out = self._build_tree(children_in, elements)
            ref = None
            # Every node returned by _extract_dom_tree is interactive
            # (filter happens at extraction time).
            if role:
                el = InteractiveElement(
                    ref=None,
                    role=role,
                    name=name,
                    tag=node.get("tag"),
                    href=node.get("href"),
                    type=node.get("type"),
                    value=node.get("value"),
                    placeholder=node.get("placeholder"),
                    checked=node.get("checked"),
                    disabled=bool(node.get("disabled", False)),
                    visible=not bool(node.get("hidden", False)),
                )
                # Attach a Playwright-resolved selector hint (validated later by Inspector).
                el.selector = self._selector_hint(node, role, name)
                elements.append(el)
            out.append(
                AccessibilityNode(
                    role=role,
                    name=name,
                    ref=ref,
                    children=children_out,
                )
            )
        return out

    @staticmethod
    def _is_interactive(role: str, node: dict[str, Any]) -> bool:
        return role.lower() in {
            "button",
            "link",
            "textbox",
            "checkbox",
            "radio",
            "combobox",
            "listbox",
            "menuitem",
            "menuitemcheckbox",
            "menuitemradio",
            "tab",
            "switch",
            "option",
            "searchbox",
            "spinbutton",
            "slider",
        } or bool(node.get("href")) or bool(node.get("onclick"))

    @staticmethod
    def _selector_hint(node: dict[str, Any], role: str, name: str) -> str | None:
        # Prefer text-based selectors; we'll validate them against the snapshot
        # in Inspector before use. Never blindly trust LLM-generated selectors.
        if role.lower() == "link" and name:
            return f"text={name}"
        if role.lower() == "button" and name:
            return f"button:has-text({name!r})"
        if node.get("href"):
            return f"a[href='{node.get('href')}']"
        return None

    @staticmethod
    def _serialize_tree(tree: list[AccessibilityNode]) -> str:
        lines: list[str] = []

        def walk(node: AccessibilityNode, depth: int) -> None:
            lines.append("  " * depth + f"- {node.role}: {node.name}")
            for c in node.children:
                walk(c, depth + 1)

        for n in tree:
            walk(n, 0)
        return "\n".join(lines)

    async def _safe_title(self, page: Page) -> str:
        try:
            return await page.title()
        except PlaywrightError:
            return ""

    # ------------------------------------------------------------------
    # Interaction primitives (SINGLE BUDGET — no sequential fallback paths)
    # ------------------------------------------------------------------
    #
    # Rebuild v4 rule: one action gets ONE bounded budget. The old
    # ``click_ref`` burned ~15s on a selector attempt, then ~15s more on a
    # role/name fallback ≈ 32s per stuck click. Strategy fallback now
    # happens ONLY on zero-match (fast, non-waiting count check), never
    # after a timeout. The bounded retry (re-resolve + one more attempt)
    # is owned by the ActionExecutor, not the driver.

    async def click_locator(self, page: Page, locator: Locator) -> bool:
        """Click a pre-built locator with the single action budget."""
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.click(timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning(
                "click failed (single budget)",
                extra={"event": "click.fail", "ms": timeout, "err": str(e)[:200]},
            )
            return False

    async def fill_locator(self, page: Page, locator: Locator, value: str) -> bool:
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.fill(value, timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning("fill failed", extra={"event": "fill.fail", "ms": timeout, "err": str(e)[:200]})
            return False

    async def select_locator(self, page: Page, locator: Locator, value: str) -> bool:
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.select_option(value, timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning("select failed", extra={"event": "select.fail", "ms": timeout, "err": str(e)[:200]})
            return False

    async def check_locator(self, page: Page, locator: Locator, checked: bool) -> bool:
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.set_checked(checked, timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning("set_checked failed", extra={"event": "check.fail", "ms": timeout, "err": str(e)[:200]})
            return False

    async def hover_locator(self, page: Page, locator: Locator) -> bool:
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.hover(timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning("hover failed", extra={"event": "hover.fail", "ms": timeout, "err": str(e)[:200]})
            return False

    async def upload_locator(self, page: Page, locator: Locator, file_path: str) -> bool:
        self.cancellation.check()
        timeout = self.settings.action_timeout_ms
        try:
            await self.cancellation.wait_or_cancel(
                locator.set_input_files(file_path, timeout=timeout),
                timeout=timeout / 1000.0 + 2.0,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log.warning("upload failed", extra={"event": "upload.fail", "ms": timeout, "err": str(e)[:200]})
            return False

    # ------------------------------------------------------------------
    # Element-based compat wrappers (single budget, NO fallback chains)
    # ------------------------------------------------------------------

    @staticmethod
    def _element_locator(page: Page, element: InteractiveElement) -> Locator | None:
        """Best single locator for an element without a registry."""
        from .element_registry import build_locator, locator_strategies

        specs = locator_strategies(element)
        for spec in specs:
            try:
                return build_locator(page, spec)
            except (ValueError, PlaywrightError):
                continue
        return None

    async def click_ref(self, page: Page, element: InteractiveElement) -> bool:
        loc = self._element_locator(page, element)
        if loc is None:
            return False
        return await self.click_locator(page, loc)

    async def fill(self, page: Page, element: InteractiveElement, value: str, *, delay: int = 0) -> bool:
        loc = self._element_locator(page, element)
        if loc is None:
            return False
        return await self.fill_locator(page, loc, value)

    async def select_option(self, page: Page, element: InteractiveElement, value: str) -> bool:
        loc = self._element_locator(page, element)
        if loc is None:
            return False
        return await self.select_locator(page, loc, value)

    async def set_checked(self, page: Page, element: InteractiveElement, checked: bool) -> bool:
        loc = self._element_locator(page, element)
        if loc is None:
            return False
        return await self.check_locator(page, loc, checked)

    async def hover(self, page: Page, element: InteractiveElement) -> bool:
        loc = self._element_locator(page, element)
        if loc is None:
            return False
        return await self.hover_locator(page, loc)

    async def scroll(self, page: Page, *, selector: str | None = None, dy: int = 400) -> bool:
        self.cancellation.check()
        try:
            if selector:
                script = f"document.querySelector({selector!r})?.scrollBy(0, {dy})"
            else:
                script = f"window.scrollBy(0, {dy})"
            await self.cancellation.wait_or_cancel(
                page.evaluate(script),
                timeout=5.0,
            )
            return True
        except PlaywrightError as e:
            log.warning("scroll failed", extra={"event": "scroll.fail", "err": str(e)})
            return False

    async def keyboard_press(self, page: Page, key: str) -> bool:
        self.cancellation.check()
        try:
            await self.cancellation.wait_or_cancel(page.keyboard.press(key), timeout=5.0)
            return True
        except PlaywrightError as e:
            log.warning("key press failed", extra={"event": "key.fail", "err": str(e)})
            return False

    async def mouse_click(self, page: Page, x: float, y: float) -> bool:
        self.cancellation.check()
        try:
            await self.cancellation.wait_or_cancel(page.mouse.click(x, y), timeout=5.0)
            return True
        except PlaywrightError as e:
            log.warning("mouse click failed", extra={"event": "mouse.fail", "err": str(e)})
            return False

    # ------------------------------------------------------------------
    # Page events wiring (called once per page by BrowserManager)
    # ------------------------------------------------------------------

    def wire_page_events(
        self,
        page: Page,
        *,
        on_request: Any | None = None,
        on_response: Any | None = None,
        on_ws: Any | None = None,
        on_console: Any | None = None,
        on_pageerror: Any | None = None,
        on_download: Any | None = None,
        on_dialog: Any | None = None,
        on_popup: Any | None = None,
    ) -> None:
        if on_request:
            page.on("request", on_request)
        if on_response:
            page.on("response", on_response)
        if on_ws:
            page.on("websocket", on_ws)
        if on_console:
            page.on("console", on_console)
        if on_pageerror:
            page.on("pageerror", on_pageerror)
        if on_download:
            page.on("download", on_download)
        if on_dialog:
            # Auto-handle dialogs to avoid blocking the loop.
            def _handle_dialog(dialog: Dialog) -> None:
                try:
                    asyncio.create_task(dialog.dismiss())
                except Exception:  # noqa: BLE001
                    pass
                if on_dialog:
                    on_dialog(dialog)
            page.on("dialog", _handle_dialog)
        if on_popup:
            page.on("popup", on_popup)

    # ------------------------------------------------------------------
    # Storage / state inspection
    # ------------------------------------------------------------------

    async def get_current_url(self, page: Page) -> str:
        try:
            return page.url
        except PlaywrightError:
            return ""

    async def get_title(self, page: Page) -> str:
        return await self._safe_title(page)

    async def list_storage_keys(self, page: Page, kind: str) -> list[str]:
        """kind in {"localStorage", "sessionStorage"}."""
        self.cancellation.check()
        try:
            return await self.cancellation.wait_or_cancel(
                page.evaluate(
                    f"() => Array.from({kind}.keys())"
                ),
                timeout=5.0,
            )
        except PlaywrightError:
            return []

    async def eval_js(self, page: Page, script: str) -> Any:
        """Gated JS evaluation. `Settings.allow_js_eval` must be True,
        otherwise this raises PermissionError."""
        if not self.settings.allow_js_eval:
            raise PermissionError("JS evaluation disabled (SBA_ALLOW_JS_EVAL=false)")
        self.cancellation.check()
        return await self.cancellation.wait_or_cancel(
            page.evaluate(script), timeout=10.0
        )

    async def find_links(self, page: Page) -> list[dict[str, str]]:
        try:
            res = await self.cancellation.wait_or_cancel(
                page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).map(a => ({href: a.href, text: (a.innerText||a.textContent||'').trim()}))"
                ),
                timeout=5.0,
            )
            return res or []
        except PlaywrightError:
            return []

    async def find_forms(self, page: Page) -> list[dict[str, Any]]:
        try:
            res = await self.cancellation.wait_or_cancel(
                page.evaluate(
                    """() => Array.from(document.forms).map(f => ({
                        action: f.action,
                        method: (f.method||'GET').toUpperCase(),
                        id: f.id || null,
                        fields: Array.from(f.elements).map(e => ({
                            tag: e.tagName.toLowerCase(),
                            type: e.type || null,
                            name: e.name || null,
                            id: e.id || null,
                            placeholder: e.placeholder || null
                        }))
                    }))"""
                ),
                timeout=5.0,
            )
            return res or []
        except PlaywrightError:
            return []

    async def detect_technologies(self, page: Page) -> list[str]:
        """Heuristic tech detection from window globals + meta + scripts."""
        try:
            res = await self.cancellation.wait_or_cancel(
                page.evaluate(
                    """() => {
                        const tech = new Set();
                        if (window.angular || document.querySelector('[ng-app],[ng-controller]')) tech.add('AngularJS');
                        if (window.ng && window.ng.getComponent) tech.add('Angular');
                        if (window.React) tech.add('React');
                        if (window.Vue) tech.add('Vue');
                        if (window.jQuery) tech.add('jQuery');
                        if (window.__NEXT_DATA__) tech.add('Next.js');
                        if (window.__NUXT__) tech.add('Nuxt');
                        if (window.__SAPPER__) tech.add('Sapper');
                        if (document.querySelector('[data-reactroot],[data-reactid]')) tech.add('React');
                        if (document.querySelector('script[src*="polyfills"]') || document.querySelector('script[src*="vendor"]')) tech.add('Angular(likely)');
                        const metas = Array.from(document.querySelectorAll('meta[name],meta[name="generator"]')).map(m => m.content||'');
                        metas.forEach(m => {
                            if (/ember/i.test(m)) tech.add('Ember');
                            if (/drupal/i.test(m)) tech.add('Drupal');
                            if (/wordpress/i.test(m)) tech.add('WordPress');
                        });
                        return Array.from(tech);
                    }"""
                ),
                timeout=5.0,
            )
            return res or []
        except PlaywrightError:
            return []
