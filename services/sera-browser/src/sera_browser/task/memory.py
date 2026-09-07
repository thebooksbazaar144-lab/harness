"""Memory + compaction for the LLM planner.

Inspired by browser-use's `long_term_memory` field + `MessageCompactionSettings`
and the survey's rolling buffer (§6.3): last ~40–50 steps full, older summarized.

The MemoryManager maintains:
- `long_term_memory`: a rolling string of cross-step facts the LLM updates
  each step (e.g. "User is logged in as admin", "Order #12345 created")
- `step_history`: a rolling list of (action, result, observation) tuples
- `summary`: a compressed summary of older steps (generated when the buffer
  exceeds `max_full_steps`)

The LLM sees both `long_term_memory` and recent `step_history` in its state
view. This keeps token cost flat regardless of task length and preserves
cross-tab facts that would otherwise vanish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger("sera.task.memory")


@dataclass
class StepRecord:
    """A single step's record for the history buffer."""
    step_num: int
    action: str
    args: dict[str, Any]
    result: str  # "ok" | "failed" | "blocked"
    detail: str = ""
    observation: str = ""
    url: str = ""


@dataclass
class MemoryManager:
    """Rolling memory + compaction for the LLM planner.

    Keeps the last `max_full_steps` steps verbatim; older steps are
    summarized into `summary` when compaction triggers.
    """

    max_full_steps: int = 40
    long_term_memory: str = ""
    step_history: list[StepRecord] = field(default_factory=list)
    summary: str = ""
    _step_counter: int = 0

    def record_step(
        self,
        *,
        action: str,
        args: dict[str, Any],
        result: str,
        detail: str = "",
        observation: str = "",
        url: str = "",
    ) -> StepRecord:
        """Record a completed step. Triggers compaction if needed."""
        self._step_counter += 1
        rec = StepRecord(
            step_num=self._step_counter,
            action=action,
            args=dict(args),
            result=result,
            detail=detail[:200],
            observation=observation[:500],
            url=url,
        )
        self.step_history.append(rec)
        # Compact if we've exceeded the full-steps window.
        if len(self.step_history) > self.max_full_steps:
            self._compact()
        return rec

    def update_long_term_memory(self, new_memory: str) -> None:
        """Replace the long-term memory field (LLM-chosen facts)."""
        old = self.long_term_memory
        self.long_term_memory = new_memory[:2000]  # cap at 2KB
        if old != self.long_term_memory:
            log.debug(
                "long_term_memory updated",
                extra={"event": "memory.ltm.update", "old_len": len(old), "new_len": len(self.long_term_memory)},
            )

    def to_view(self, max_recent: int = 10) -> dict[str, Any]:
        """Compact JSON-safe view for the LLM state view."""
        recent = self.step_history[-max_recent:]
        return {
            "long_term_memory": self.long_term_memory,
            "summary_of_older_steps": self.summary,
            "recent_steps": [
                {
                    "step": r.step_num,
                    "action": r.action,
                    "result": r.result,
                    "detail": r.detail,
                    "url": r.url,
                }
                for r in recent
            ],
            "total_steps_recorded": len(self.step_history) + (
                int(self.summary.split(" ")[0]) if self.summary.startswith("Compacted") and self.summary.split(" ")[0].isdigit() else 0
            ),
        }

    def _compact(self) -> None:
        """Compact older steps into a summary.

        We keep the most recent `max_full_steps` steps and summarize the rest
        into a short string. The summary is intentionally lossy — it records
        action counts + URLs visited + any failures, so the LLM has context
        without the full detail.
        """
        if len(self.step_history) <= self.max_full_steps:
            return
        # Summarize the oldest 25% of the full buffer.
        n_to_compact = max(1, len(self.step_history) // 4)
        old_steps = self.step_history[:n_to_compact]
        self.step_history = self.step_history[n_to_compact:]
        # Build a lossy summary.
        actions: dict[str, int] = {}
        urls: set[str] = set()
        failures = 0
        for s in old_steps:
            actions[s.action] = actions.get(s.action, 0) + 1
            if s.url:
                urls.add(s.url)
            if s.result == "failed":
                failures += 1
        action_summary = ", ".join(f"{k}×{v}" for k, v in sorted(actions.items()))
        url_list = list(urls)[:5]
        prev_compacted = 0
        if self.summary.startswith("Compacted "):
            parts = self.summary.split(" ")
            if len(parts) >= 2 and parts[1].isdigit():
                prev_compacted = int(parts[1])
        self.summary = (
            f"Compacted {prev_compacted + n_to_compact} older steps: "
            f"actions [{action_summary}], "
            f"visited {len(urls)} urls ({', '.join(url_list)}), "
            f"{failures} failures."
        )
        log.info(
            "memory compacted",
            extra={
                "event": "memory.compact",
                "compacted": n_to_compact,
                "total": prev_compacted + n_to_compact,
                "remaining": len(self.step_history),
            },
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_steps": len(self.step_history),
            "long_term_memory_chars": len(self.long_term_memory),
            "summary_chars": len(self.summary),
            "max_full_steps": self.max_full_steps,
        }


__all__ = ["MemoryManager", "StepRecord"]
