"""ElementRegistry — the single authoritative element-reference namespace.

This module is the Phase-4 core of the browser-intelligence rebuild. It
replaces the previous THREE incompatible reference systems (driver
``role#N:name`` refs, inspector ``linkN``/``form1_text_email`` fabricated
refs, and synthesized ``button_submit_*`` refs) with ONE registry that:

1. Assigns stable, opaque refs (``e001``, ``e002``, ...) to interactive
   elements at observation time.
2. Preserves ref identity across snapshots via content signatures, so the
   planner/LLM mental model stays consistent while the DOM changes.
3. Never stores Playwright ``ElementHandle`` objects as agent identity.
   A ref resolves to a FRESH Playwright ``Locator`` on every operation —
   Playwright locators re-resolve on each use and provide auto-waiting and
   actionability checks, which is exactly what we want.
4. Tombstones refs for elements that disappear, and never recycles a ref
   for a different element identity within a task.
5. Invalidates refs after repeated resolution/action failures so the
   planner cannot keep re-issuing dead actions.
6. Emits the compact LLM element view (refs + roles + names) and the
   machine-readable locator specs for evidence.

Locator strategy order (per element, all DATA, never handles):

    1. role+name   (get_by_role)     — semantic, robust to DOM churn
    2. css href    (a[href=...])     — links
    3. placeholder (get_by_placeholder)
    4. label       (get_by_label)
    5. text        (get_by_text)
    6. css selector hint from the DOM walker

Strategy fallback happens ONLY when a strategy matches ZERO elements
(a fast, non-waiting ``count()`` roundtrip). A strategy that matches but
times out during the action consumes the action budget and fails — it
never falls through to another strategy, which is what previously caused
~30s stuck clicks (15s selector path + 15s role/name path).
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError, Locator, Page

from ..logging import get_logger
from ..models import InteractiveElement

log = get_logger("sera.browser.element_registry")

# Roles emitted by the DOM walker that are valid ARIA role names for
# Playwright's get_by_role. "generic" and tag-derived pseudo roles are
# excluded.
_ARIA_ROLES = {
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
}

# Refuse resolution for refs whose failure count reaches this ceiling —
# the element is considered dead for the rest of the task.
_FAILURE_INVALIDATE_THRESHOLD = 3


def content_signature(el: InteractiveElement) -> str:
    """Stable identity hash of an element's observable content."""
    raw = f"{el.role}|{el.name}|{el.tag or ''}|{el.href or ''}|{el.type or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # nosec: B324


def locator_strategies(el: InteractiveElement) -> list[dict[str, Any]]:
    """Ordered locator SPECS for an element (pure data, no handles)."""
    specs: list[dict[str, Any]] = []
    role = (el.role or "").strip().lower()
    name = (el.name or "").strip()
    if role in _ARIA_ROLES and name:
        specs.append({"type": "role", "role": role, "name": name[:120]})
    if el.href:
        specs.append({"type": "css_href", "href": el.href})
    if el.placeholder:
        specs.append({"type": "placeholder", "placeholder": el.placeholder[:120]})
    if el.tag == "input" and name:
        specs.append({"type": "label", "text": name[:120]})
    if name and not specs:
        specs.append({"type": "text", "text": name[:120]})
    if el.selector:
        specs.append({"type": "css", "selector": el.selector})
    return specs


@dataclass
class ElementRecord:
    """Live registry record for one element."""

    ref: str
    element: InteractiveElement
    signature: str
    strategies: list[dict[str, Any]]
    first_seen_version: int
    last_seen_version: int
    invalidated: bool = False
    invalidation_reason: str = ""
    failure_count: int = 0
    # Number of completed interactions (clicks/fills) on this ref.
    interaction_count: int = 0

    def view(self) -> dict[str, Any]:
        el = self.element
        return {
            "ref": self.ref,
            "role": el.role,
            "name": el.name[:80],
            "tag": el.tag,
            "type": el.type,
            "href": (el.href[:100] if el.href else None),
            "placeholder": el.placeholder,
            "disabled": el.disabled,
            # Primary locator spec — machine-readable, used in evidence.
            "locator": self.strategies[0] if self.strategies else None,
        }


@dataclass
class RegistrySnapshot:
    """Result of one registry observation."""

    version: int
    elements: list[InteractiveElement]
    new_refs: list[str] = field(default_factory=list)
    lost_refs: list[str] = field(default_factory=list)
    changed: bool = False


def build_locator(page: Page, spec: dict[str, Any]) -> Locator:
    """Build a FRESH Playwright Locator from a locator spec.

    Locators are lazy: they re-resolve against the live DOM on every
    operation, giving us Playwright's built-in auto-waiting and
    actionability checks. We never cache ElementHandles.
    """
    t = spec.get("type")
    if t == "role":
        return page.get_by_role(spec["role"], name=spec["name"], exact=False).first
    if t == "css_href":
        href = spec["href"].replace("'", "\\'")
        return page.locator(f"a[href='{href}']").first
    if t == "placeholder":
        return page.get_by_placeholder(spec["placeholder"], exact=False).first
    if t == "label":
        return page.get_by_label(spec["text"], exact=False).first
    if t == "text":
        return page.get_by_text(spec["text"], exact=False).first
    if t == "css":
        return page.locator(spec["selector"]).first
    raise ValueError(f"unknown locator spec type: {t!r}")


class ElementRegistry:
    """The authoritative element-reference namespace for one task.

    Lifecycle per step:
        observe(elements) -> RegistrySnapshot   (assign/refresh refs)
        planner picks refs from registry.view()
        resolve(page, ref) -> Locator           (fresh, per operation)
        action executes with a bounded budget
        mark_failed(ref) / invalidate(ref)      (feedback into registry)
    """

    def __init__(self) -> None:
        self.version = 0
        self._next_id = itertools.count(1)
        # Live records: ref -> record (element present in the last snapshot).
        self._records: dict[str, ElementRecord] = {}
        # Records by content signature for identity preservation.
        self._by_signature: dict[str, str] = {}
        # Tombstones: ref -> version at which the element disappeared.
        self._tombstones: dict[str, int] = {}
        # Signatures of tombstoned refs (unlocked if the identity returns).
        self._dead_signatures: set[str] = set()
        # Signatures of EXPLICITLY INVALIDATED refs — permanent for the
        # task; never unlocked, never re-minted.
        self._invalidated_signatures: set[str] = set()
        self._invalidated_total = 0

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(self, elements: list[InteractiveElement]) -> RegistrySnapshot:
        """Register a fresh snapshot's elements; assign/refresh refs.

        Identity preservation: an element whose content signature matches a
        live record keeps that record's ref. Elements absent from this
        snapshot are tombstoned (their refs stop resolving) but a ref is
        never reused for a different element identity. An element that
        disappears and later returns with the same signature gets a NEW
        ref (the old ref stays dead) — minting is cheap and safety matters
        more than ref-number economy.
        """
        self.version += 1
        ver = self.version
        seen_refs: set[str] = set()
        new_refs: list[str] = []
        # Deduplicate by content signature (first occurrence wins).
        incoming: dict[str, InteractiveElement] = {}
        for el in elements:
            incoming.setdefault(content_signature(el), el)

        # First pass: refresh live records whose signature still matches.
        for sig, el in incoming.items():
            if sig in self._invalidated_signatures:
                continue  # explicitly invalidated — permanent for this task
            ref = self._by_signature.get(sig)
            if ref is not None and ref in self._records:
                rec = self._records[ref]
                rec.element = el
                rec.last_seen_version = ver
                # Re-derive strategies (name/href may have drifted).
                rec.strategies = locator_strategies(el)
                el.ref = ref
                seen_refs.add(ref)
            elif sig in self._dead_signatures:
                # Returning identity after absence: unlock the signature so
                # the second pass mints a fresh ref. The old ref stays dead.
                self._dead_signatures.discard(sig)

        # Second pass: mint fresh refs for new identities.
        for sig, el in incoming.items():
            if self._by_signature.get(sig) in seen_refs:
                continue  # refreshed in the first pass
            if sig in self._invalidated_signatures:
                continue  # explicitly invalidated — never re-minted
            if sig in self._dead_signatures:
                continue  # still dead (locked by a different record)
            ref = f"e{next(self._next_id):03d}"
            el.ref = ref
            self._records[ref] = ElementRecord(
                ref=ref,
                element=el,
                signature=sig,
                strategies=locator_strategies(el),
                first_seen_version=ver,
                last_seen_version=ver,
            )
            self._by_signature[sig] = ref
            seen_refs.add(ref)
            new_refs.append(ref)

        # Tombstone records that vanished from this snapshot.
        lost_refs: list[str] = []
        for ref, rec in list(self._records.items()):
            if ref not in seen_refs:
                lost_refs.append(ref)
                self._tombstones[ref] = ver
                self._dead_signatures.add(rec.signature)
                del self._records[ref]
                self._by_signature.pop(rec.signature, None)

        return RegistrySnapshot(
            version=ver,
            elements=[rec.element for rec in self._records.values()],
            new_refs=new_refs,
            lost_refs=lost_refs,
            changed=bool(new_refs or lost_refs),
        )

    # ------------------------------------------------------------------
    # Lookup / resolution
    # ------------------------------------------------------------------

    def get(self, ref: str | None) -> ElementRecord | None:
        if not ref:
            return None
        rec = self._records.get(ref)
        if rec is None or rec.invalidated:
            return None
        return rec

    def current_elements(self) -> list[InteractiveElement]:
        return [rec.element for rec in self._records.values() if not rec.invalidated]

    async def resolve(self, page: Page, ref: str | None) -> Locator | None:
        """Resolve a ref to a FRESH Locator for the current DOM.

        Strategy fallback uses a fast, non-waiting ``count()`` roundtrip:
        strategies that match ZERO elements are skipped instantly. When a
        strategy matches, its locator is returned and the CALLER applies a
        bounded timeout — Playwright's auto-wait handles actionability.
        Returns None if the ref is unknown, tombstoned, invalidated, or no
        strategy matches anything on the page.
        """
        rec = self.get(ref)
        if rec is None:
            return None
        for spec in rec.strategies:
            try:
                loc = build_locator(page, spec)
            except (ValueError, PlaywrightError):
                continue
            if await element_exists(loc):
                return loc
        return None

    def build_locators(self, page: Page, ref: str | None) -> list[Locator]:
        """All locator candidates for a ref (ordered), without existence
        checks. Used when the caller wants to manage fallback timing
        itself (e.g. the ActionExecutor's bounded retry)."""
        rec = self.get(ref)
        if rec is None:
            return []
        out: list[Locator] = []
        for spec in rec.strategies:
            try:
                out.append(build_locator(page, spec))
            except (ValueError, PlaywrightError):
                continue
        return out

    def locator_specs(self, ref: str) -> list[dict[str, Any]]:
        rec = self.get(ref)
        return list(rec.strategies) if rec else []

    # ------------------------------------------------------------------
    # Feedback / lifecycle
    # ------------------------------------------------------------------

    def mark_failed(self, ref: str | None, reason: str = "") -> None:
        """Record a resolution/action failure on a ref.

        After ``_FAILURE_INVALIDATE_THRESHOLD`` failures the ref is
        permanently invalidated so the planner cannot keep re-issuing
        dead actions.
        """
        rec = self._records.get(ref) if ref else None
        if rec is None or rec.invalidated:
            return
        rec.failure_count += 1
        if rec.failure_count >= _FAILURE_INVALIDATE_THRESHOLD:
            self.invalidate(ref, reason=f"failures={rec.failure_count}")

    def invalidate(self, ref: str | None, reason: str = "") -> None:
        """Permanently retire a ref for this task."""
        rec = self._records.get(ref) if ref else None
        if rec is None or rec.invalidated:
            return
        rec.invalidated = True
        rec.invalidation_reason = reason
        self._invalidated_total += 1
        # Remove from live resolution and permanently lock the signature —
        # an explicitly invalidated element never gets a new ref, even if
        # it keeps appearing in snapshots.
        sig = rec.signature
        if self._by_signature.get(sig) == ref:
            self._by_signature.pop(sig, None)
        del self._records[ref]
        self._tombstones[ref] = self.version
        self._invalidated_signatures.add(sig)
        log.info(
            "element ref invalidated",
            extra={"event": "registry.invalidate", "ref": ref, "reason": reason},
        )

    def mark_interacted(self, ref: str | None) -> None:
        rec = self._records.get(ref) if ref else None
        if rec is not None:
            rec.interaction_count += 1

    def is_valid(self, ref: str | None) -> bool:
        return self.get(ref) is not None

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def view(self, max_elements: int = 60) -> list[dict[str, Any]]:
        """Compact LLM view: ref + role + name + hints."""
        out: list[dict[str, Any]] = []
        for rec in list(self._records.values())[:max_elements]:
            out.append(rec.view())
        return out

    def serialized(self, max_elements: int = 80) -> str:
        """browser-use-style indexed element list for LLM prompts."""
        lines: list[str] = []
        for rec in list(self._records.values())[:max_elements]:
            el = rec.element
            attrs: list[str] = []
            if el.tag:
                attrs.append(el.tag)
            if el.type:
                attrs.append(f"type={el.type}")
            if el.placeholder:
                attrs.append(f"placeholder={el.placeholder[:40]}")
            if el.disabled:
                attrs.append("disabled")
            attr_str = " ".join(attrs)
            name_str = f' "{el.name[:60]}"' if el.name else ""
            tag_str = f"<{attr_str} />" if attr_str else ""
            lines.append(f"[{rec.ref}] {el.role}{name_str} {tag_str}".rstrip())
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "live_elements": len(self._records),
            "tombstoned": len(self._tombstones),
            "invalidated_total": getattr(self, "_invalidated_total", 0),
            "total_interactions": sum(
                r.interaction_count for r in self._records.values()
            ),
        }

    # ------------------------------------------------------------------
    # Compat: refmap-style helpers
    # ------------------------------------------------------------------

    def find_by_ref(self, ref: str) -> InteractiveElement | None:
        rec = self.get(ref)
        return rec.element if rec else None


def asyncio_count_sync(loc: Locator) -> int:
    """count() is a coroutine; the registry's resolve() is sync by design
    (it returns the locator, not a task). We cannot await here, so callers
    that need the existence check use ``element_exists`` below instead.
    """
    raise NotImplementedError("internal misuse — use element_exists()")


async def element_exists(loc: Locator, timeout_ms: int = 1500) -> bool:
    """Fast, bounded existence check for a locator (no auto-wait)."""
    import asyncio

    try:
        count = await asyncio.wait_for(loc.count(), timeout=timeout_ms / 1000.0)
        return count > 0
    except (asyncio.TimeoutError, PlaywrightError):
        return False


__all__ = [
    "ElementRegistry",
    "ElementRecord",
    "RegistrySnapshot",
    "content_signature",
    "locator_strategies",
    "build_locator",
    "element_exists",
]
