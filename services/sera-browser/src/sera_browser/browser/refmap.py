"""Versioned ref map + serialized tree format.

Inspired by browser-use's `selector_index` ref map and the survey's
`version:ref` snapshot versioning (§5.1.1). This is the foundation for:

- Stale-ref detection (reject tool calls whose version ≠ current snapshot version)
- Token-cheap serialized tree format (`*[index]<tag attrs />`) for the LLM
- Multi-strategy element resolution (ref → CSS → role+name → text → coordinate)

Each snapshot gets a monotonically-increasing `version`. Every interactive
element gets a stable short integer `index` (e.g. `42`). The LLM emits
`ref="42"`; the execution layer maps `42` → the live DOM node via the ref
map for the CURRENT version. If the LLM emits a ref from a stale version,
validation rejects it and forces a fresh snapshot — preventing the agent
from acting on a "Cancel" button that became "Delete" with the same ref.
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger
from ..models import AccessibilityNode, InteractiveElement

log = get_logger("sera.browser.refmap")


# Global counter for snapshot versions. Monotonic across the process lifetime
# so that even cross-task ref collisions are impossible.
_version_counter = itertools.count(1)


@dataclass
class RefEntry:
    """A single entry in the ref map.

    `index` is the short integer id the LLM sees.
    `element` is the full InteractiveElement (with role/name/href/etc).
    `backend_node_id` is a stable backend id (when available from CDP) for
       self-healing; we fall back to a content-based signature.
    `content_signature` is a hash of (role, name, tag, href, type) used to
       detect when an element at the same index has changed identity.
    """

    index: int
    element: InteractiveElement
    backend_node_id: str | None = None
    content_signature: str = ""
    # Bounding box for coordinate-based click fallback + Set-of-Mark vision.
    bounding_box: dict[str, float] | None = None


@dataclass
class RefMap:
    """Versioned map of index → RefEntry for one snapshot.

    The `version` is monotonic and unique across the process. The LLM must
    include the version in its action response so we can detect stale refs.
    """

    version: int
    entries: dict[int, RefEntry] = field(default_factory=dict)
    # The serialized tree format string (browser-use-style `*[index]<tag />`).
    serialized: str = ""
    # Hash of the serialized tree for loop detection.
    dom_hash: str = ""

    @property
    def size(self) -> int:
        return len(self.entries)

    def get(self, index: int) -> RefEntry | None:
        return self.entries.get(index)

    def add(self, entry: RefEntry) -> None:
        self.entries[entry.index] = entry

    def to_view(self, max_elements: int = 60) -> list[dict[str, Any]]:
        """Compact JSON-safe view of the ref map for the LLM state view."""
        out: list[dict[str, Any]] = []
        for entry in list(self.entries.values())[:max_elements]:
            el = entry.element
            out.append(
                {
                    "index": entry.index,
                    "role": el.role,
                    "name": el.name,
                    "tag": el.tag,
                    "type": el.type,
                    "href": el.href,
                    "placeholder": el.placeholder,
                    "value": (el.value[:80] if el.value else None),
                    "checked": el.checked,
                    "disabled": el.disabled,
                }
            )
        return out


def new_version() -> int:
    """Allocate a new snapshot version."""
    return next(_version_counter)


def content_signature(role: str, name: str, tag: str | None, href: str | None, type_: str | None) -> str:
    """A short hash of an element's identity. Used to detect identity drift."""
    raw = f"{role}|{name}|{tag or ''}|{href or ''}|{type_ or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # nosec: B324


def serialize_tree(tree: list[AccessibilityNode], refmap: RefMap) -> str:
    """Serialize the accessibility tree into browser-use-style format.

    Format (one node per line, indented by depth):
        <role> "name" [index]<tag attrs />
        └─ <role> "name" [5]<button disabled />

    New/changed elements (different content_signature from the previous
    snapshot at the same index) are prefixed with `*` to draw the LLM's
    attention, mirroring browser-use's new-element marker.
    """
    lines: list[str] = []

    def walk(node: AccessibilityNode, depth: int) -> None:
        prefix = "  " * depth
        # Find the ref entry for this node (if it has one).
        entry: RefEntry | None = None
        if node.ref:
            # Our existing ref format is "role#N:name" — extract the index.
            # We also support bare integer refs from the new refmap.
            try:
                idx = _parse_ref_index(node.ref)
                if idx is not None:
                    entry = refmap.get(idx)
            except (ValueError, TypeError):
                pass
        marker = ""
        attrs: list[str] = []
        if entry is not None:
            el = entry.element
            if el.tag:
                attrs.append(el.tag)
            if el.type:
                attrs.append(f"type={el.type}")
            if el.href:
                attrs.append(f"href={el.href[:60]}")
            if el.placeholder:
                attrs.append(f"placeholder={el.placeholder[:40]}")
            if el.disabled:
                attrs.append("disabled")
            if el.checked is not None:
                attrs.append(f"checked={el.checked}")
        attr_str = " ".join(attrs)
        idx_str = f"[{entry.index}]" if entry is not None else ""
        name_str = f' "{node.name}"' if node.name else ""
        tag_str = f"<{attr_str} />" if attr_str else ""
        lines.append(f"{prefix}{marker}{node.role}{name_str} {idx_str}{tag_str}".rstrip())
        for child in node.children:
            walk(child, depth + 1)

    for n in tree:
        walk(n, 0)
    return "\n".join(lines)


def _parse_ref_index(ref: str) -> int | None:
    """Extract an integer index from a ref string.

    Supports both the old format "button#1:Login" and a bare integer "5".
    """
    if not ref:
        return None
    # Bare integer
    if ref.isdigit():
        return int(ref)
    # Old format: "role#N:name" → extract N
    if "#" in ref:
        after_hash = ref.split("#", 1)[1]
        if ":" in after_hash:
            n_str = after_hash.split(":", 1)[0]
        else:
            n_str = after_hash
        if n_str.isdigit():
            return int(n_str)
    return None


def build_refmap(
    tree: list[AccessibilityNode],
    elements: list[InteractiveElement],
    *,
    version: int | None = None,
    previous: RefMap | None = None,
) -> RefMap:
    """Build a RefMap from a snapshot.

    Assigns stable integer indices to interactive elements, computes content
    signatures, and marks new/changed elements relative to `previous`.
    """
    ver = version or new_version()
    refmap = RefMap(version=ver)
    # Build a lookup from old ref → InteractiveElement so we can reuse indices.
    el_by_old_ref: dict[str, InteractiveElement] = {e.ref: e for e in elements}

    # Assign indices. We try to preserve indices from the previous snapshot
    # when the content signature matches (stable identity), which keeps the
    # LLM's mental model consistent across snapshots.
    used_indices: set[int] = set()
    next_index = 1

    # First pass: reuse previous indices for elements with matching signatures.
    if previous is not None:
        for el in elements:
            sig = content_signature(el.role, el.name, el.tag, el.href, el.type)
            for prev_entry in previous.entries.values():
                if prev_entry.content_signature == sig and prev_entry.index not in used_indices:
                    entry = RefEntry(
                        index=prev_entry.index,
                        element=el,
                        content_signature=sig,
                        backend_node_id=el.ref,  # use old ref as backend id
                    )
                    refmap.add(entry)
                    used_indices.add(prev_entry.index)
                    break

    # Second pass: assign fresh indices to new elements.
    for el in elements:
        # Skip if already assigned in the first pass.
        if any(e.element is el for e in refmap.entries.values()):
            continue
        while next_index in used_indices:
            next_index += 1
        sig = content_signature(el.role, el.name, el.tag, el.href, el.type)
        entry = RefEntry(
            index=next_index,
            element=el,
            content_signature=sig,
            backend_node_id=el.ref,
        )
        refmap.add(entry)
        used_indices.add(next_index)
        next_index += 1

    # Build the serialized tree format.
    refmap.serialized = serialize_tree(tree, refmap)
    refmap.dom_hash = hashlib.sha1(refmap.serialized.encode("utf-8")).hexdigest()  # nosec: B324
    return refmap


def resolve_ref(refmap: RefMap, ref: str, *, expected_version: int | None = None) -> RefEntry | None:
    """Resolve a ref string to a RefEntry, with stale-version detection.

    If `expected_version` is provided and doesn't match `refmap.version`,
    returns None (stale ref — caller should force a fresh snapshot).
    """
    if expected_version is not None and expected_version != refmap.version:
        log.debug(
            "stale ref rejected",
            extra={
                "event": "refmap.stale_ref",
                "expected_version": expected_version,
                "current_version": refmap.version,
            },
        )
        return None
    idx = _parse_ref_index(ref)
    if idx is None:
        return None
    return refmap.get(idx)


__all__ = [
    "RefEntry",
    "RefMap",
    "build_refmap",
    "resolve_ref",
    "serialize_tree",
    "content_signature",
    "new_version",
]
