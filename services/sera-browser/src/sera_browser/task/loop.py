"""LoopRecoveryManager — multi-signal loop detection + target blocking.

Rebuild v4. Loop detection is no longer just "the same tool call string
repeated 4 times". It watches SEVEN signals:

1. **Tool repeat**      — same (action, key) issued consecutively.
2. **Failure repeat**   — same (action, key, failure) failing repeatedly.
3. **URL stagnation**   — page URL unchanged across actions that were
                          supposed to navigate.
4. **DOM stagnation**   — dom_hash unchanged after state-changing actions.
5. **Target blocking**  — explicitly blocked refs/routes accumulate.
6. **LLM output repeat**— the planner keeps emitting the same step
                          (detected by the planner, recorded here).
7. **Observation churn**— observe-only steps (screenshot/extract/noop)
                          without progress — the old screenshot↔observe
                          ping-pong.

Recovery is TARGET ABANDONMENT, never "capture a screenshot": when a loop
is detected, the current target (ref or route) is added to the blocked
set, and the planner must select a DIFFERENT unexplored target. Screenshots
are evidence, not a loop-escape strategy.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from ..logging import get_logger
from ..models import BrowserState

log = get_logger("sera.task.loop")


LoopSignal = Literal[
    "tool_repeat",
    "failure_repeat",
    "url_stagnation",
    "dom_stagnation",
    "observation_churn",
]

_OBSERVE_ONLY_ACTIONS = {"screenshot", "extract", "noop", "history", "search_page", "find_elements", "scrape", "content"}


@dataclass
class LoopRecoveryManager:
    tool_repeat_limit: int = 2
    semantic_loop_limit: int = 3
    # Same (action, key) failing this many times blocks the target.
    failure_block_limit: int = 2

    _tool_calls: deque[str] = field(default_factory=lambda: deque(maxlen=24))
    _tool_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _dom_hash_history: deque[str] = field(default_factory=lambda: deque(maxlen=24))
    # v4: failure tracking per (action, key).
    _failure_counts: dict[tuple[str, str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    # v4: blocked targets — refs and routes that must never be re-picked.
    blocked_refs: set[str] = field(default_factory=set)
    blocked_routes: set[str] = field(default_factory=set)
    # v4: URL history for stagnation detection.
    _url_history: deque[str] = field(default_factory=lambda: deque(maxlen=24))
    # v4: observation-only churn counter.
    _observation_churn: int = 0
    # Diagnostics: loop episodes handled (for the report).
    loop_episodes: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_tool(self, name: str, args: str = "") -> None:
        key = f"{name}({args})"
        self._tool_calls.append(key)
        self._tool_counts[key] += 1
        if name in _OBSERVE_ONLY_ACTIONS:
            self._observation_churn += 1
        else:
            self._observation_churn = 0

    def record_failure(self, action: str, key: str, failure_kind: str) -> None:
        """Record an action failure; blocks the target after the limit."""
        fk = (action, key, failure_kind)
        self._failure_counts[fk] += 1
        if self._failure_counts[fk] >= self.failure_block_limit:
            if action in ("navigate", "new_tab") and key:
                self.block_route(key)
            elif key and key not in ("()", ""):
                self.block_ref(key)
            self._episode(
                "failure_repeat",
                action=action,
                key=key,
                failure=failure_kind,
                blocked=key,
            )

    def record_dom_hash(self, dom_hash: str | None) -> None:
        if dom_hash:
            self._dom_hash_history.append(dom_hash)

    def record_url(self, url: str | None) -> None:
        if url:
            self._url_history.append(url)

    # ------------------------------------------------------------------
    # Blocking (target abandonment)
    # ------------------------------------------------------------------

    def block_ref(self, ref: str) -> None:
        if ref not in self.blocked_refs:
            self.blocked_refs.add(ref)
            log.warning(
                "target blocked (ref)",
                extra={"event": "loop.block.ref", "ref": ref},
            )

    def block_route(self, route_or_url: str) -> None:
        if route_or_url not in self.blocked_routes:
            self.blocked_routes.add(route_or_url)
            log.warning(
                "target blocked (route)",
                extra={"event": "loop.block.route", "route": route_or_url[:120]},
            )

    def is_ref_blocked(self, ref: str | None) -> bool:
        return bool(ref) and ref in self.blocked_refs

    def is_route_blocked(self, route_or_url: str | None) -> bool:
        return bool(route_or_url) and (
            route_or_url in self.blocked_routes or route_or_url.rstrip("/") in {
                r.rstrip("/") for r in self.blocked_routes
            }
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def tool_loop(self) -> str | None:
        """Return the tool call string that has repeated too many times, else None."""
        if not self._tool_calls:
            return None
        last = list(self._tool_calls)[-self.tool_repeat_limit :]
        if len(last) == self.tool_repeat_limit and len(set(last)) == 1:
            log.warning(
                "tool repetition loop detected",
                extra={"event": "loop.tool", "tool": last[0], "count": self.tool_repeat_limit},
            )
            return last[0]
        return None

    def semantic_loop(self, state: BrowserState) -> bool:
        """True if the dom_hash hasn't changed after `semantic_loop_limit`
        consecutive observations."""
        if len(self._dom_hash_history) < self.semantic_loop_limit:
            return False
        last = list(self._dom_hash_history)[-self.semantic_loop_limit :]
        if len(set(last)) == 1:
            log.warning(
                "semantic loop detected (dom_hash unchanged)",
                extra={"event": "loop.semantic", "hash": last[0][:10]},
            )
            return True
        return False

    def url_stagnation(self, state: BrowserState) -> bool:
        """URL unchanged across the last N state-changing actions."""
        if len(self._url_history) < self.semantic_loop_limit:
            return False
        last = list(self._url_history)[-self.semantic_loop_limit :]
        return len(set(last)) == 1

    def observation_churn(self) -> bool:
        """Too many consecutive observation-only steps without progress."""
        return self._observation_churn >= self.semantic_loop_limit

    def detect(self, state: BrowserState) -> LoopSignal | None:
        """Highest-priority loop signal, or None."""
        if self.tool_loop() is not None:
            return "tool_repeat"
        # Failure-repeat blocking happens inline in record_failure.
        if self.semantic_loop(state) and self.url_stagnation(state):
            return "dom_stagnation"
        if self.observation_churn():
            return "observation_churn"
        return None

    def escape(self, state: BrowserState, current_ref: str | None = None) -> LoopSignal | None:
        """Detect a loop AND abandon the current target.

        This is THE loop-escape mechanism: block the current target, reset
        the short-term detectors, and let the planner select a different
        unexplored target. NEVER a screenshot — screenshots do not change
        the DOM and cannot break a loop.
        """
        signal = self.detect(state)
        if signal is None:
            return None
        # Abandon whatever we're currently pointed at.
        if current_ref:
            self.block_ref(current_ref)
        repeated = self.tool_loop()
        if repeated:
            # e.g. "click(e004)" → block e004.
            inner = repeated[repeated.find("(") + 1 : -1]
            if inner and inner not in ("", "()"):
                if inner.startswith("http") or inner.startswith("/"):
                    self.block_route(inner)
                else:
                    self.block_ref(inner)
        self._episode(signal, repeated=repeated, blocked=current_ref)
        # Reset short-term detectors so detection doesn't instantly re-fire
        # while the planner legitimately explores the next target.
        self.reset_step()
        self.reset_tool_history()
        self.reset_dom_history()
        self._observation_churn = 0
        return signal

    def _episode(self, signal: str, **kw: object) -> None:
        self.loop_episodes.append({"signal": signal, **kw})

    # ------------------------------------------------------------------
    # Compat / reset API
    # ------------------------------------------------------------------

    def reset_step(self) -> None:
        """Clear per-step counters (call after a successful action)."""
        self._tool_counts.clear()

    def reset_tool_history(self) -> None:
        """Clear the recent tool-call history."""
        self._tool_calls.clear()
        self._tool_counts.clear()

    def reset_dom_history(self) -> None:
        """Clear the dom-hash history."""
        self._dom_hash_history.clear()

    def suggest_recovery(self, state: BrowserState) -> str:
        """Compat string API (kept for older callers).

        The v4 string for every loop signal is now ``abandon_target`` —
        the caller must select a different unexplored target, not a
        screenshot.
        """
        if self.tool_loop() or self.semantic_loop(state):
            return "abandon_target"
        if self.url_stagnation(state) or self.observation_churn():
            return "abandon_target"
        return "continue"

    def snapshot(self) -> dict:
        return {
            "tool_calls": list(self._tool_calls),
            "tool_counts": dict(self._tool_counts),
            "dom_hash_history": [h[:10] for h in self._dom_hash_history],
            "blocked_refs": sorted(self.blocked_refs),
            "blocked_routes": sorted(self.blocked_routes),
            "failure_counts": {
                f"{a}|{k}|{f}": c for (a, k, f), c in self._failure_counts.items()
            },
            "loop_episodes": list(self.loop_episodes[-10:]),
        }


__all__ = ["LoopRecoveryManager", "LoopSignal"]
