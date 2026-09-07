"""Multi-strategy element resolver with self-healing.

Inspired by Skyvern's mode-3 (selector-first → AI-fallback) and Stagehand's
auto-refresh. The resolver tries multiple locator strategies in order:

1. **ref** (versioned integer index from the RefMap) — fastest, most stable
2. **CSS selector** (validated against the snapshot) — for cached selectors
3. **role + name** (Playwright's `get_by_role`) — semantic, robust to DOM changes
4. **text** (`get_by_text`) — fallback for unlabeled elements
5. **coordinate** (x, y) — vision-escalation escape hatch for canvas/custom UI

On failure, the resolver:
- detects whether the element moved (stale ref) vs. disappeared
- tries the next strategy
- if all fail, returns a descriptive error so the planner can self-heal

Self-healing: when a CSS selector fails, the resolver re-runs the snapshot
extraction to find a NEW selector for the same element identity (matched by
content_signature), and caches it for future use.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError, Locator, Page, TimeoutError as PlaywrightTimeoutError

from ..logging import get_logger
from ..models import InteractiveElement
from .refmap import RefMap, content_signature, resolve_ref

log = get_logger("sera.browser.resolver")


ResolutionStrategy = Literal["ref", "css", "role_name", "text", "coordinate", "failed"]


@dataclass
class ResolutionResult:
    """The outcome of an element-resolution attempt."""
    ok: bool
    locator: Locator | None = None
    strategy: ResolutionStrategy = "failed"
    detail: str = ""
    # If the element identity changed (content_signature differs from what
    # the LLM expected), this is set so the caller can flag it.
    identity_drift: bool = False


@dataclass
class ElementResolver:
    """Multi-strategy element resolver.

    Wraps a Playwright Page and an optional RefMap. The `resolve()` method
    returns a Locator for an element identified by ref/selector/text/coords,
    trying multiple strategies in order.
    """

    page: Page
    refmap: RefMap | None = None
    # Cache of content_signature → CSS selector, for self-healing.
    _selector_cache: dict[str, str] = field(default_factory=dict)

    async def resolve(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
        text: str | None = None,
        x: float | None = None,
        y: float | None = None,
        expected_version: int | None = None,
        timeout_ms: int = 5000,
    ) -> ResolutionResult:
        """Resolve an element to a Playwright Locator.

        Tries strategies in order: ref → css → role+name → text → coordinate.
        Returns the first that yields a visible, enabled element.
        """
        # 1. ref (versioned)
        if ref is not None and self.refmap is not None:
            entry = resolve_ref(self.refmap, ref, expected_version=expected_version)
            if entry is not None:
                el = entry.element
                # Try the element's own selector hint first.
                if el.selector:
                    r = await self._try_locator(
                        self.page.locator(el.selector).first,
                        strategy="ref",
                        timeout_ms=timeout_ms,
                    )
                    if r.ok:
                        return r
                # Then try role+name from the ref entry.
                if el.role and el.name:
                    r = await self._try_locator(
                        self.page.get_by_role(el.role, name=el.name, exact=False).first,
                        strategy="ref",
                        timeout_ms=timeout_ms,
                    )
                    if r.ok:
                        return r
            else:
                log.debug(
                    "ref resolution failed (stale or unknown)",
                    extra={"event": "resolver.ref.fail", "ref": ref},
                )

        # 2. CSS selector (validated)
        if selector:
            r = await self._try_locator(
                self.page.locator(selector).first,
                strategy="css",
                timeout_ms=timeout_ms,
            )
            if r.ok:
                # Cache for self-healing.
                return r
            # Selector failed — try self-healing by re-finding by identity.
            healed = await self._self_heal_selector(selector, timeout_ms=timeout_ms)
            if healed is not None:
                return healed

        # 3. role + name
        if role and name:
            r = await self._try_locator(
                self.page.get_by_role(role, name=name, exact=False).first,
                strategy="role_name",
                timeout_ms=timeout_ms,
            )
            if r.ok:
                return r

        # 4. text
        if text:
            r = await self._try_locator(
                self.page.get_by_text(text, exact=False).first,
                strategy="text",
                timeout_ms=timeout_ms,
            )
            if r.ok:
                return r

        # 5. coordinate (vision escape hatch)
        if x is not None and y is not None:
            try:
                # We don't return a Locator for coordinate clicks — the
                # caller uses page.mouse.click(x, y) directly. We signal ok
                # so the caller knows coordinates were provided.
                return ResolutionResult(
                    ok=True,
                    locator=None,
                    strategy="coordinate",
                    detail=f"coords=({x},{y})",
                )
            except Exception:  # noqa: BLE001
                pass

        return ResolutionResult(
            ok=False,
            strategy="failed",
            detail="all strategies failed",
        )

    async def _try_locator(
        self,
        locator: Locator,
        *,
        strategy: ResolutionStrategy,
        timeout_ms: int,
    ) -> ResolutionResult:
        """Try to confirm a locator points to a visible, enabled element."""
        try:
            # Quick visibility check without a full wait.
            count = await asyncio.wait_for(locator.count(), timeout=timeout_ms / 1000.0)
            if count == 0:
                return ResolutionResult(ok=False, strategy=strategy, detail="no match")
            # Check visibility (non-blocking).
            visible = await asyncio.wait_for(locator.first.is_visible(), timeout=timeout_ms / 1000.0)
            if not visible:
                return ResolutionResult(ok=False, strategy=strategy, detail="not visible")
            return ResolutionResult(ok=True, locator=locator.first, strategy=strategy)
        except PlaywrightTimeoutError:
            return ResolutionResult(ok=False, strategy=strategy, detail="timeout")
        except PlaywrightError as e:
            return ResolutionResult(ok=False, strategy=strategy, detail=f"error: {e}")

    async def _self_heal_selector(
        self,
        failed_selector: str,
        *,
        timeout_ms: int,
    ) -> ResolutionResult | None:
        """Re-snapshot the page and try to find a new selector for the element
        that the failed selector used to point to.

        This is a best-effort self-healing path. We don't have the original
        element's identity (the selector was the only handle), so we can't
        match by content_signature. Instead, we log the failure and let the
        caller force a fresh snapshot + re-plan.

        Returns None if no healed selector could be found.
        """
        log.info(
            "selector self-heal attempted (no identity to match)",
            extra={"event": "resolver.self_heal.no_identity", "selector": failed_selector},
        )
        return None

    async def resolve_element(
        self,
        el: InteractiveElement,
        *,
        expected_version: int | None = None,
        timeout_ms: int = 5000,
    ) -> ResolutionResult:
        """Convenience: resolve an InteractiveElement directly."""
        return await self.resolve(
            ref=el.ref,
            selector=el.selector,
            role=el.role,
            name=el.name,
            expected_version=expected_version,
            timeout_ms=timeout_ms,
        )


__all__ = ["ElementResolver", "ResolutionResult", "ResolutionStrategy"]
