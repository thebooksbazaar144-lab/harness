"""Action failure classification — the shared vocabulary for recovery.

Rebuild v4: every failed action is classified into exactly one failure
kind. The kind determines the recovery strategy deterministically (no LLM
guessing):

===========================  =====================================  ==========================
Failure kind                 Meaning                                Recovery
===========================  =====================================  ==========================
``unknown_ref``              ref not in current registry            none — planner re-picks
``stale_ref``                ref tombstoned / invalidated           re-observe, planner re-picks
``not_found``                locator matched nothing on the page    re-observe + one retry
``timeout``                  element present but not actionable     one retry after re-resolve
``not_actionable``           element disabled / hidden              block target, move on
``detached``                 element vanished mid-action            re-observe + one retry
``nav_rejected``             SSRF / allowlist rejection             none — planner re-picks
``provider_error``           LLM/vision provider failure            circuit breaker, no loop
``browser_disconnected``     browser/page died mid-action           clean task termination
``exception``                unexpected error                       classify, continue task
===========================  =====================================  ==========================

The ``retry_recommended`` flag drives the single bounded retry in the
ActionExecutor: a failure may be retried AT MOST once, and only after a
fresh resolution. A failed action is never reissued indefinitely —
failure counts feed the LoopRecoveryManager, which blocks the target.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FailureKind = Literal[
    "unknown_ref",
    "stale_ref",
    "not_found",
    "timeout",
    "not_actionable",
    "detached",
    "nav_rejected",
    "provider_error",
    "browser_disconnected",
    "exception",
]

# Failures worth ONE bounded retry (after fresh resolution).
_RETRYABLE: set[str] = {"not_found", "timeout", "detached"}


@dataclass(frozen=True)
class ActionFailure:
    kind: FailureKind
    detail: str = ""
    retry_recommended: bool = False

    def __post_init__(self) -> None:
        if self.kind in _RETRYABLE and not self.retry_recommended:
            object.__setattr__(self, "retry_recommended", True)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail[:300],
            "retry_recommended": self.retry_recommended,
        }


def classify_exception(exc: Exception) -> ActionFailure:
    """Deterministic classification from a raw exception."""
    # Import lazily: playwright may not be importable in unit tests.
    try:
        from playwright.async_api import Error as PlaywrightError, TimeoutError as PWTimeout

        if isinstance(exc, PWTimeout):
            return ActionFailure(kind="timeout", detail=str(exc)[:200])
        if isinstance(exc, PlaywrightError):
            msg = str(exc).lower()
            if "target closed" in msg or "browser has been closed" in msg or "page closed" in msg:
                return ActionFailure(kind="browser_disconnected", detail=str(exc)[:200])
            if "not attached" in msg or "detached" in msg:
                return ActionFailure(kind="detached", detail=str(exc)[:200])
            if "element is not visible" in msg or "disabled" in msg or "hidden" in msg:
                return ActionFailure(kind="not_actionable", detail=str(exc)[:200])
            return ActionFailure(kind="exception", detail=str(exc)[:200])
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, PermissionError):
        return ActionFailure(kind="nav_rejected", detail=str(exc)[:200])
    return ActionFailure(kind="exception", detail=str(exc)[:200])


__all__ = ["ActionFailure", "FailureKind", "classify_exception"]
