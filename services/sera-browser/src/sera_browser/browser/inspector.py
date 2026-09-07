"""Inspector — DOM + accessibility inspection, form/link matching.

Rebuild v4: the Inspector no longer fabricates references. It:

- Snapshots via PlaywrightDriver WITH the ElementRegistry, so every
  interactive element carries the single authoritative ref namespace.
- Matches form fields to live registry elements (by name / placeholder /
  id-ish heuristics) instead of synthesizing ``form1_text_email`` refs.
- Sets ``FormDescriptor.submit_ref`` ONLY to a real, live registry ref —
  never to a made-up ``button_submit_2`` string.
- Sets ``LinkDescriptor.ref`` to the registry ref of the matching anchor
  element when one exists, else None (report-only).

SERA compatibility: ``Inspector(driver=...)`` (the SERA import surface)
still works and creates an internal registry automatically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page

from ..logging import get_logger
from ..models import (
    AccessibilityNode,
    FormDescriptor,
    InteractiveElement,
    LinkDescriptor,
)
from .driver import PlaywrightDriver
from .element_registry import ElementRegistry

log = get_logger("sera.browser.inspector")


def _href_matches(el_href: str | None, link_href: str) -> bool:
    """Does a snapshot element's RAW href point at the same target as the
    (absolute) link href?

    The DOM walker captures the raw attribute (``/login``, ``#/login``)
    while find_links captures the absolute URL
    (``http://host/login``, ``http://host/#/login``).
    """
    if not el_href:
        return False
    if el_href == link_href:
        return True
    if el_href.startswith("#"):
        # Hash link: the absolute href ends with "#/login".
        return link_href.endswith("#" + el_href) or link_href.endswith(el_href)
    if el_href.startswith("/"):
        # Path link: the absolute href ends with the path.
        return link_href.endswith(el_href)
    return False


@dataclass
class SnapshotSummary:
    url: str
    title: str
    dom_hash: str
    elements: list[InteractiveElement]
    tree: list[AccessibilityNode]
    forms: list[FormDescriptor]
    links: list[LinkDescriptor]
    routes: list[str]
    snapshot_ms: float
    raw: str = ""
    # Registry bookkeeping (v4): version + which refs are new/lost.
    snapshot_version: int = 0
    new_refs: list[str] | None = None
    lost_refs: list[str] | None = None


class Inspector:
    def __init__(self, driver: PlaywrightDriver, registry: ElementRegistry | None = None) -> None:
        self.driver = driver
        # The registry is THE authoritative ref namespace. If none is
        # supplied (SERA's construction pattern), create a private one —
        # refs remain registry-shaped and consistent.
        self.registry = registry if registry is not None else ElementRegistry()

    @property
    def element_registry(self) -> ElementRegistry:
        return self.registry

    async def observe(self, page: Page) -> SnapshotSummary:
        """Capture snapshot + DOM-derived forms/links/routes."""
        started = time.perf_counter()
        snap = await self.driver.snapshot(page, registry=self.registry)
        elements = snap.elements

        # Forms: prefer JS form inspection for richer fields; match refs
        # against the live registry — NO fabricated references.
        raw_forms = await self.driver.find_forms(page)
        forms = self._build_form_descriptors(raw_forms, elements)

        # Links: match descriptors to live anchor elements.
        raw_links = await self.driver.find_links(page)
        links = self._build_link_descriptors(raw_links, elements, page.url)

        # Routes: derived from internal links + visible URLs.
        routes = self._derive_routes(links, page.url)

        dur = (time.perf_counter() - started) * 1000.0
        reg_snap_version = self.registry.version
        log.debug(
            "observe done",
            extra={
                "event": "inspect.done",
                "elements": len(elements),
                "forms": len(forms),
                "links": len(links),
                "routes": len(routes),
                "registry_version": reg_snap_version,
                "ms": round(dur, 1),
            },
        )
        return SnapshotSummary(
            url=snap.url,
            title=snap.title,
            dom_hash=snap.dom_hash,
            elements=elements,
            tree=snap.tree,
            forms=forms,
            links=links,
            routes=routes,
            snapshot_ms=dur,
            raw=snap.raw,
            snapshot_version=reg_snap_version,
        )

    # ------------------------------------------------------------------
    # Form descriptors — registry-matched, never fabricated
    # ------------------------------------------------------------------

    def _build_form_descriptors(
        self,
        raw_forms: list[dict[str, Any]],
        elements: list[InteractiveElement],
    ) -> list[FormDescriptor]:
        out: list[FormDescriptor] = []
        for idx, f in enumerate(raw_forms):
            fields: list[InteractiveElement] = []
            has_password = False
            has_csrf = False
            form_action = f.get("action")
            for el in f.get("fields", []):
                tag = str(el.get("tag", "") or "").lower()
                type_ = str(el.get("type", "") or "").lower() or None
                name = el.get("name") or el.get("id") or el.get("placeholder") or tag
                name = str(name or tag)
                # Match to a LIVE registry element: the field is then
                # actionable by its registry ref. Matching precedence:
                # placeholder > exact name > tag+type.
                matched = self._match_field_element(
                    el, type_=type_, elements=elements
                )
                if matched is not None:
                    fields.append(matched)
                else:
                    # Unmatched: descriptive-only element, ref stays None.
                    fields.append(
                        InteractiveElement(
                            ref=None,
                            role=self._field_role(tag, type_),
                            name=name,
                            tag=tag or None,
                            type=type_,
                            placeholder=el.get("placeholder"),
                        )
                    )
                if type_ == "password":
                    has_password = True
                if name and ("csrf" in name.lower() or "token" in name.lower()):
                    has_csrf = True
            # submit_ref: ONLY a real, live registry ref. The old code
            # fabricated `button_submit_{idx}` strings that could never
            # resolve — those are gone.
            submit_ref = self._match_submit_element(elements)
            form_ref = f"form{idx + 1}"
            # The form descriptor's own ref is a REPORT identifier (not an
            # element ref); it is namespaced as `form:N` to make its role
            # unambiguous. It is never valid as an action target.
            out.append(
                FormDescriptor(
                    ref=form_ref,
                    action=form_action,
                    method=f.get("method", "GET"),
                    fields=fields,
                    has_password=has_password,
                    has_csrf=has_csrf,
                    submit_ref=submit_ref,
                )
            )
        return out

    @staticmethod
    def _field_role(tag: str, type_: str | None) -> str:
        if tag == "input" and type_ in {"text", "email", "password", "tel", "url"}:
            return "textbox"
        if tag == "input" and type_ == "checkbox":
            return "checkbox"
        if tag == "input" and type_ == "radio":
            return "radio"
        if tag == "select":
            return "combobox"
        if tag == "textarea":
            return "textbox"
        if tag == "input" and type_ in {"submit", "button"}:
            return "button"
        return tag or "generic"

    @staticmethod
    def _match_field_element(
        field: dict[str, Any],
        *,
        type_: str | None,
        elements: list[InteractiveElement],
    ) -> InteractiveElement | None:
        """Find the live registry element corresponding to a form field."""
        placeholder = field.get("placeholder")
        name = field.get("name") or field.get("id")
        tag = str(field.get("tag", "") or "").lower()
        for el in elements:
            if not el.ref:
                continue  # not registry-live
            if el.tag != tag:
                continue
            if type_ and el.type and el.type.lower() != type_.lower():
                continue
            if placeholder and el.placeholder and el.placeholder == placeholder:
                return el
            if name and (el.name == name or (el.placeholder == name)):
                return el
        # Last resort: same tag+type singleton.
        if tag and type_:
            candidates = [
                el
                for el in elements
                if el.ref and el.tag == tag and (el.type or "").lower() == type_.lower()
            ]
            if len(candidates) == 1:
                return candidates[0]
        return None

    @staticmethod
    def _match_submit_element(elements: list[InteractiveElement]) -> str | None:
        """A submit-ish BUTTON that is actually present in the snapshot."""
        for el in elements:
            if not el.ref or el.role != "button":
                continue
            low = (el.name or "").lower()
            if any(k in low for k in ("submit", "login", "log in", "sign in", "register", "continue")):
                return el.ref
        return None

    # ------------------------------------------------------------------
    # Link descriptors — registry-matched
    # ------------------------------------------------------------------

    @staticmethod
    def _build_link_descriptors(
        raw_links: list[dict[str, str]],
        elements: list[InteractiveElement],
        current_url: str,
    ) -> list[LinkDescriptor]:
        out: list[LinkDescriptor] = []
        cur_host = urlparse(current_url).netloc
        seen: set[str] = set()
        for l in raw_links:
            href = l.get("href") or ""
            text = (l.get("text") or "").strip()[:80]
            if not href or href.startswith("javascript:") or href in seen:
                continue
            seen.add(href)
            host = urlparse(href).netloc
            internal = (not host) or host == cur_host
            # Registry match: an anchor element with this href. The DOM
            # walker stores the RAW attribute (e.g. "/login" or "#/login")
            # while find_links returns the ABSOLUTE URL — compare both
            # exact and suffix-normalized forms.
            ref = next(
                (el.ref for el in elements if el.ref and _href_matches(el.href, href)),
                None,
            )
            out.append(
                LinkDescriptor(
                    ref=ref,
                    href=href,
                    text=text,
                    internal=internal,
                )
            )
        return out

    @staticmethod
    def _derive_routes(links: list[LinkDescriptor], current_url: str) -> list[str]:
        """Extract unique internal route keys from links.

        Handles BOTH routing styles:
        - Path routing: href "/products" → route "/products".
        - Hash routing (Angular HashLocationStrategy, older SPAs):
          href "http://host/#/login" → route "/login" (the fragment path).
        """
        routes: list[str] = []
        seen: set[str] = set()
        for l in links:
            if not l.internal:
                continue
            parsed = urlparse(l.href)
            # Hash-route first: fragment "/login" from "http://host/#/login".
            route = None
            if parsed.fragment.startswith("/"):
                frag_path = urlparse(f"scheme://x{parsed.fragment}").path or parsed.fragment
                route = frag_path or parsed.fragment
            elif parsed.path and parsed.path != "/":
                route = parsed.path
            if route and route != "/" and route not in seen:
                seen.add(route)
                routes.append(route)
        # Dedupe + sort for deterministic output
        return sorted(routes)

    def validate_selector(self, selector: str, elements: list[InteractiveElement]) -> bool:
        """LLM-suggested selectors must resolve to an element in the snapshot.

        We accept selectors that match a known text/name/role.
        """
        if not selector:
            return False
        # Text selectors
        if selector.startswith("text="):
            wanted = selector[len("text=") :].strip()
            for e in elements:
                if wanted and wanted in e.name:
                    return True
            return False
        # Role-based: role=button[name="Sign in"]
        if selector.startswith("role="):
            return True  # trust Playwright's role resolver, then validate by click
        # CSS: accept only if it's a known-safe form (href, id, type)
        return any(
            e.href and e.href in selector
            or (e.tag and e.tag in selector and e.name in selector)
            for e in elements
        )

    def find_by_ref(self, ref: str, elements: list[InteractiveElement]) -> InteractiveElement | None:
        for e in elements:
            if e.ref == ref:
                return e
        return None
