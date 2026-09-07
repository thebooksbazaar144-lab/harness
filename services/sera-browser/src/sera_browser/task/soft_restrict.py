"""Soft action allowlist (dsh-browser `browser_restrict` inspired).

A model-controllable guardrail that restricts which browser actions are
allowed. Unlike the hard `SafetyGuard`, this is a SOFT guardrail — the
model can lift it itself with an empty list. It's for preventing accidental
actions, not for security.

Read-only actions (navigate, back, forward, reload, scroll, screenshot,
snapshot, content, search_page, find_elements, history, extract) are
never blocked by the soft restrict — only mutation actions are gated.

Usage:
    restrict = SoftRestrict()
    restrict.set_restriction(["navigate", "click", "fill"])  # allow these + read-only
    restrict.set_restriction([])  # lift the restriction
    if not restrict.is_allowed("fill_form"):
        raise PermissionError("action 'fill_form' is restricted")
"""
from __future__ import annotations

from ..logging import get_logger

log = get_logger("sera.task.soft_restrict")


# Actions that are always allowed (read-only / lifecycle).
_READONLY_ACTIONS = frozenset({
    "navigate",
    "back",
    "forward",
    "reload",
    "scroll",
    "screenshot",
    "snapshot",
    "content",
    "search_page",
    "find_elements",
    "history",
    "extract",
    "scrape",
    "finish",
    "blocked",
    "noop",
    "update_memory",
    "restrict",  # always allowed so the guard can be lifted
    "session",
    "flush_auth",  # read cookies (still sensitive, but not a mutation)
})


class SoftRestrict:
    """Soft action allowlist — model-controllable guardrail."""

    def __init__(self) -> None:
        self._allowed: set[str] | None = None  # None = unrestricted

    def set_restriction(self, allowed: list[str]) -> None:
        """Set the allow-list. Empty list lifts the restriction."""
        if not allowed:
            self._allowed = None
        else:
            self._allowed = set(allowed)
        log.info(
            "soft restrict updated",
            extra={
                "event": "soft_restrict.set",
                "allowed": sorted(self._allowed) if self._allowed else "(unrestricted)",
            },
        )

    def is_allowed(self, action: str) -> bool:
        """Check if an action is allowed under the current restriction."""
        # Read-only actions are always allowed.
        if action in _READONLY_ACTIONS:
            return True
        # No restriction → everything allowed.
        if self._allowed is None:
            return True
        return action in self._allowed

    @property
    def restricted(self) -> bool:
        """True if a restriction is in effect."""
        return self._allowed is not None

    @property
    def allowed(self) -> list[str] | None:
        """The current allow-list, or None if unrestricted."""
        return sorted(self._allowed) if self._allowed is not None else None


__all__ = ["SoftRestrict", "_READONLY_ACTIONS"]
