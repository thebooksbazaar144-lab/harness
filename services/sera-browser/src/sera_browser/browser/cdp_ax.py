"""CDP-based accessibility tree extractor.

Uses Chrome DevTools Protocol's `Accessibility.getFullAXTree` to get the
native accessibility tree, which is richer than our DOM-based walker:
- exposes ARIA semantics (aria-label, aria-expanded, aria-level, etc.)
- includes computed properties (focused, selected, invalid)
- pierces shadow DOM
- returns backendNodeId for stable element resolution

This is the SOTA approach used by browser-use and the Playwright MCP server.

Falls back to the DOM-based extractor if:
- CDP is unavailable (non-Chromium browsers)
- the page is not yet loaded
- the AX tree is empty (rare; happens on canvas-only pages)

Usage:
    extractor = CDPAccessibilityExtractor(page)
    tree = await extractor.extract()
    # tree is a list of AccessibilityNode dicts with:
    #   {role, name, backendNodeId, properties, children, ...}
"""
from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from ..logging import get_logger

log = get_logger("sera.browser.cdp_ax")


# ARIA roles that are interactive and should get a ref.
_INTERACTIVE_ROLES = {
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
    "treeitem",
    "menuitem",
    "menu",
    "menubar",
    "tablist",
    "tree",
    "treegrid",
    "grid",
    "row",
    "cell",
    "columnheader",
    "rowheader",
    "dialog",
    "alertdialog",
}


class CDPAccessibilityExtractor:
    """Extracts the accessibility tree via CDP.

    Requires a Chromium-based browser (Chrome/Edge). For Firefox/WebKit,
    use the DOM-based extractor in PlaywrightDriver._extract_dom_tree().
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    async def extract(self) -> list[dict[str, Any]]:
        """Extract the full AX tree via CDP.

        Returns a list of root nodes (usually one: the page's root). Each
        node has:
            role: str
            name: str
            backendNodeId: int (stable backend id for element resolution)
            properties: dict (ARIA state: focused, expanded, checked, etc.)
            children: list[dict] (recursive)
            interactive: bool (True if role is in _INTERACTIVE_ROLES)

        Returns [] on any failure — callers should fall back to DOM extraction.
        """
        try:
            cdp_session = await self.page.context.new_cdp_session(self.page)
        except (PlaywrightError, Exception) as e:  # noqa: BLE001
            log.debug(
                "CDP session creation failed (non-Chromium browser?)",
                extra={"event": "cdp_ax.session.fail", "err": str(e)},
            )
            return []
        try:
            # Enable the Accessibility domain.
            await cdp_session.send("Accessibility.enable")
            # Get the full AX tree.
            result = await cdp_session.send("Accessibility.getFullAXTree")
            ax_nodes = result.get("nodes", [])
            if not ax_nodes:
                log.debug("CDP AX tree is empty", extra={"event": "cdp_ax.empty"})
                return []
            # Build a parent→children map and find roots.
            return self._build_tree(ax_nodes)
        except (PlaywrightError, Exception) as e:  # noqa: BLE001
            log.debug(
                "CDP AX extraction failed",
                extra={"event": "cdp_ax.extract.fail", "err": str(e)},
            )
            return []
        finally:
            try:
                await cdp_session.detach()
            except Exception:  # noqa: BLE001
                pass

    def _build_tree(self, ax_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a tree from CDP's flat node list.

        CDP returns a flat list of nodes with `nodeId`, `parentId`, and
        `childIds`. We convert this to a nested tree and extract the
        fields we care about.
        """
        # Index nodes by nodeId.
        by_id: dict[str, dict[str, Any]] = {n["nodeId"]: n for n in ax_nodes}
        # Find roots (nodes with no parent or parent not in the list).
        roots: list[dict[str, Any]] = []
        for n in ax_nodes:
            parent_id = n.get("parentId")
            if not parent_id or parent_id not in by_id:
                roots.append(n)
        if not roots:
            # Fallback: treat the first node as root.
            roots = [ax_nodes[0]] if ax_nodes else []
        # Recursively build the tree.
        return [self._convert_node(n, by_id) for n in roots]

    def _convert_node(
        self, cdp_node: dict[str, Any], by_id: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Convert a CDP AX node to our normalized format."""
        role_obj = cdp_node.get("role", {})
        role = role_obj.get("value", "") if isinstance(role_obj, dict) else str(role_obj)
        name_obj = cdp_node.get("name", {})
        name = name_obj.get("value", "") if isinstance(name_obj, dict) else str(name_obj)
        backend_node_id = cdp_node.get("backendNodeId")
        # Extract properties (focused, expanded, checked, disabled, etc.).
        properties: dict[str, Any] = {}
        for prop in cdp_node.get("properties", []):
            name_p = prop.get("name")
            value_p = prop.get("value", {})
            if name_p and isinstance(value_p, dict):
                properties[name_p] = value_p.get("value")
        # Extract HTML attributes (href, type, placeholder, value, etc.).
        attrs: dict[str, Any] = {}
        for attr in cdp_node.get("htmlAttributes", []):
            name_a = attr.get("name")
            value_a = attr.get("value")
            if name_a:
                attrs[name_a] = value_a
        # Build children.
        children: list[dict[str, Any]] = []
        for child_id in cdp_node.get("childIds", []):
            child = by_id.get(child_id)
            if child is not None:
                children.append(self._convert_node(child, by_id))
        interactive = role.lower() in _INTERACTIVE_ROLES
        return {
            "role": role,
            "name": name,
            "backendNodeId": backend_node_id,
            "properties": properties,
            "htmlAttributes": attrs,
            "interactive": interactive,
            "children": children,
        }


def cdp_nodes_to_elements(
    tree: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten the CDP AX tree into a list of interactive elements.

    Returns nodes with: role, name, backendNodeId, href, type, placeholder,
    checked, disabled, value — compatible with InteractiveElement construction.
    """
    out: list[dict[str, Any]] = []
    seen_backends: set[int] = set()

    def walk(node: dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("interactive"):
            backend = node.get("backendNodeId")
            if backend is not None and backend not in seen_backends:
                seen_backends.add(backend)
                props = node.get("properties", {})
                attrs = node.get("htmlAttributes", {})
                out.append(
                    {
                        "role": node.get("role", ""),
                        "name": node.get("name", ""),
                        "backendNodeId": backend,
                        "href": attrs.get("href"),
                        "type": attrs.get("type"),
                        "placeholder": attrs.get("placeholder"),
                        "value": attrs.get("value"),
                        "checked": props.get("checked"),
                        "disabled": bool(props.get("disabled", False)),
                        "expanded": props.get("expanded"),
                        "focused": props.get("focused"),
                    }
                )
        for child in node.get("children", []):
            walk(child)

    for root in tree:
        walk(root)
    return out


__all__ = ["CDPAccessibilityExtractor", "cdp_nodes_to_elements"]
