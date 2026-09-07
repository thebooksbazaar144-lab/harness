"""Intelligent snapshot trimming.

Inspired by the survey §6.2 (lightweight-model snapshot trimmer) and
browser-use's below-the-fold hints. We don't have a second LLM to trim,
so we use deterministic heuristics that preserve all interactive elements:

1. **Always keep** all interactive elements (refs)
2. **Always keep** the first N items of repetitive lists (e.g. product grids)
3. **Truncate** long text content (names, placeholders) to a max length
4. **Drop** non-interactive structural nodes beyond a depth limit
5. **Cap** the total serialized size at `max_chars` (default 50KB)

The output is a trimmed serialized tree + a note telling the LLM how many
elements were hidden, so it knows to scroll if it needs them.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..logging import get_logger
from ..models import AccessibilityNode, InteractiveElement

log = get_logger("sera.browser.trimmer")


_MAX_NAME_LEN = 80
_MAX_PLACEHOLDER_LEN = 60
_MAX_HREF_LEN = 100
_DEFAULT_MAX_CHARS = 50_000
_DEFAULT_MAX_LIST_ITEMS = 5
_DEFAULT_MAX_DEPTH = 8


@dataclass
class TrimResult:
    """The result of trimming a snapshot."""
    serialized: str
    original_chars: int
    trimmed_chars: int
    elements_kept: int
    elements_hidden: int
    note: str = ""


def trim_snapshot(
    tree: list[AccessibilityNode],
    elements: list[InteractiveElement],
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_list_items: int = _DEFAULT_MAX_LIST_ITEMS,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> TrimResult:
    """Trim a snapshot to fit within `max_chars` while keeping all interactive elements.

    Strategy:
    - Walk the tree, building a serialized string.
    - Keep every node that has a ref (interactive).
    - For non-interactive list-like containers, keep only the first
      `max_list_items` children + a "[N more hidden]" note.
    - Truncate long name/href/placeholder strings.
    - Stop if we hit `max_chars`; record how many elements were hidden.
    """
    lines: list[str] = []
    elements_kept = 0
    elements_hidden = 0
    original_chars = 0

    # First pass: compute the original size for stats.
    def _count(node: AccessibilityNode) -> int:
        n = len(node.name) + len(node.role) + 4
        for c in node.children:
            n += _count(c)
        return n
    for n in tree:
        original_chars += _count(n)

    # Second pass: build the trimmed string.
    def walk(node: AccessibilityNode, depth: int) -> bool:
        """Returns True if we should continue, False if we hit the cap."""
        nonlocal elements_kept, elements_hidden
        if depth > max_depth:
            return True
        prefix = "  " * depth
        name = node.name[:_MAX_NAME_LEN] if node.name else ""
        # Check if this node is interactive (has a ref).
        is_interactive = node.ref is not None
        if is_interactive:
            elements_kept += 1
        # Check if children form a repetitive list (same role).
        children = node.children
        trimmed_children = children
        list_note = ""
        if (
            len(children) > max_list_items * 2
            and len({c.role for c in children}) <= 2
            and not is_interactive
        ):
            # Looks like a repetitive list — keep only the first N.
            elements_hidden_in_list = len(children) - max_list_items
            trimmed_children = children[:max_list_items]
            list_note = f" [{elements_hidden_in_list} more hidden]"
            # Count hidden interactive elements.
            elements_hidden += sum(1 for c in children[max_list_items:] if c.ref is not None)
        line = f"{prefix}{node.role}"
        if name:
            line += f' "{name}"'
        if node.ref:
            line += f" [{node.ref}]"
        line += list_note
        lines.append(line)
        for child in trimmed_children:
            if not walk(child, depth + 1):
                return False
        return True

    for n in tree:
        if not walk(n, 0):
            break

    serialized = "\n".join(lines)
    if len(serialized) > max_chars:
        # Hard cap — truncate and note.
        hidden_after_cap = elements_kept - serialized[:max_chars].count("[")
        serialized = serialized[:max_chars] + f"\n... (trimmed {len(serialized) - max_chars} chars)"
        elements_hidden += max(0, hidden_after_cap)
        note = f"Snapshot trimmed to {max_chars} chars; ~{elements_hidden} elements hidden."
    elif elements_hidden > 0:
        note = f"{elements_hidden} non-interactive list items hidden (use scroll to see more)."
    else:
        note = ""
    return TrimResult(
        serialized=serialized,
        original_chars=original_chars,
        trimmed_chars=len(serialized),
        elements_kept=elements_kept,
        elements_hidden=elements_hidden,
        note=note,
    )


__all__ = ["trim_snapshot", "TrimResult"]
