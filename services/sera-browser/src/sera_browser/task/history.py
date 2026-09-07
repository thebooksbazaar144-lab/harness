"""History + Replay manager (dsh-browser inspired).

Records every browser operation into a bounded chronological log. Supports
replay of replayable actions (navigate/click/type/scroll/key/execute).
Sensitive fields (typed text, set values, execute scripts) are masked in
the model-facing output to prevent credential leakage.

The history manager is wired into ActionExecutor by the BrowserAgent.
Each operation is recorded with:
    seq: monotonic sequence number
    action: the action name
    params: the action's arguments (sensitive fields preserved for replay,
            masked in model-facing output)
    ok: whether the operation succeeded
    result: truncated result text (masked for execute)
    error: error text if failed
    at: epoch millis

Replay re-issues navigate/click/type/scroll/key/execute against the current
page. Non-replayable actions (fill, screenshot, scrape, etc.) raise a
clear error.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger("sera.task.history")


# Actions that can be replayed. Others (fill, screenshot, scrape, history,
# replay, restrict, flush_auth, restore_auth) are not replayable.
_REPLAYABLE_ACTIONS = frozenset({
    "navigate",
    "click",
    "fill",  # single fill is replayable
    "select",
    "check",
    "uncheck",
    "hover",
    "scroll",
    "press",
    "back",
    "forward",
    "reload",
    "evaluate",
})

# Sensitive fields that must be masked in model-facing output.
_SENSITIVE_FIELDS = {"text", "value", "script", "password", "token"}


@dataclass
class HistoryEntry:
    """A single recorded browser operation."""
    seq: int
    action: str
    params: dict[str, Any]
    ok: bool
    result: str | None = None
    error: str | None = None
    at: float = 0.0

    def to_masked_dict(self) -> dict[str, Any]:
        """Return a model-safe view with sensitive fields masked."""
        params = dict(self.params)
        for key in list(params.keys()):
            if key in _SENSITIVE_FIELDS and isinstance(params[key], str):
                length = len(params[key])
                params[key] = "*" * min(length, 64) + f" ({length} chars)"
        out: dict[str, Any] = {
            "seq": self.seq,
            "action": self.action,
            "ok": self.ok,
            "params": params,
        }
        if self.result is not None:
            # Mask execute results.
            if self.action == "evaluate":
                out["result"] = f"[{len(self.result)} chars redacted]"
            else:
                out["result"] = self.result[:200]
        if self.error is not None:
            out["error"] = self.error[:200]
        out["at"] = self.at
        return out


@dataclass
class HistoryManager:
    """Records browser operations + supports replay.

    Bounded to the last `max_entries` operations (default 500).
    """
    max_entries: int = 500
    _entries: list[HistoryEntry] = field(default_factory=list)
    _next_seq: int = 1

    def record(
        self,
        action: str,
        params: dict[str, Any],
        ok: bool,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> HistoryEntry:
        """Record an operation. Returns the entry."""
        entry = HistoryEntry(
            seq=self._next_seq,
            action=action,
            params=dict(params),
            ok=ok,
            result=result,
            error=error,
            at=time.time(),
        )
        self._next_seq += 1
        self._entries.append(entry)
        # Bound the log.
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]
        return entry

    def list_entries(self) -> list[HistoryEntry]:
        """Return all entries (newest last)."""
        return list(self._entries)

    def get_entry(self, seq: int) -> HistoryEntry | None:
        """Get one entry by sequence number."""
        for e in self._entries:
            if e.seq == seq:
                return e
        return None

    def masked_view(self) -> list[dict[str, Any]]:
        """Return all entries as masked dicts (model-safe)."""
        return [e.to_masked_dict() for e in self._entries]

    async def replay(self, seq: int, page: Any, executor: Any) -> None:
        """Replay a recorded operation by sequence number.

        Re-issues the action against the current page via the executor.
        The replayed step is appended to history as a new entry.
        """
        entry = self.get_entry(seq)
        if entry is None:
            raise ValueError(f"no history entry with seq {seq}")
        if entry.action not in _REPLAYABLE_ACTIONS:
            raise ValueError(
                f"history seq {seq} action {entry.action!r} is not replayable"
            )
        # Re-issue via the executor.
        result = await executor.execute(
            page,
            entry.params.get("state", {}),  # state placeholder
            entry.params.get("loop", None),  # loop placeholder
            entry.action,
            **{k: v for k, v in entry.params.items() if k not in ("state", "loop")},
        )
        # Record the replay as a new entry.
        self.record(
            "replay",
            {"seq": seq, "of": entry.action, **entry.params},
            result.ok,
            result=result.detail,
        )
        log.info(
            "replayed operation",
            extra={
                "event": "history.replay",
                "seq": seq,
                "action": entry.action,
                "ok": result.ok,
            },
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_entries": len(self._entries),
            "max_entries": self.max_entries,
            "next_seq": self._next_seq,
        }


__all__ = ["HistoryManager", "HistoryEntry"]
