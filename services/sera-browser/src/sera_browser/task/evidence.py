"""EvidenceLog — structured per-step evidence (rebuild v4, Phase 14).

Every executed step produces one ``StepEvidence`` record: the action and
its arguments (redacted), the classified outcome, the network requests
attributed to that action (watermark delta), the navigation change, and
the screenshot path when one exists. The log is:

- appended by the agent loop (never by the executor — one writer),
- serialized into the AgentReport (bounded) and saved to the session
  directory as ``evidence.jsonl`` (full),
- the raw material SERA experts consume for controlled verification.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger

log = get_logger("sera.task.evidence")

_MAX_ARG_VALUE = 200
_MAX_EVIDENCE_IN_REPORT = 120

_SENSITIVE_ARG_KEYS = {"value", "password", "script", "cookies", "memory"}


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if isinstance(v, str):
            if k.lower() in _SENSITIVE_ARG_KEYS:
                out[k] = f"<redacted:{len(v)} chars>"
            else:
                out[k] = v[:_MAX_ARG_VALUE]
        elif isinstance(v, list) and k == "fields":
            # fill_form fields: keep refs, redact values.
            out[k] = [
                {f.get("ref"): "<redacted>"} if isinstance(f, dict) else str(f)[:50]
                for f in v[:20]
            ]
        else:
            out[k] = v
    return out


@dataclass
class StepEvidence:
    step: int
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    failure: dict[str, Any] | None = None
    detail: str = ""
    ref: str | None = None
    url_before: str | None = None
    url_after: str | None = None
    route_changed: bool = False
    attributed_requests: list[dict[str, Any]] = field(default_factory=list)
    screenshot: str | None = None
    data_keys: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["args"] = _redact_args(self.args)
        d["attributed_requests"] = self.attributed_requests[:40]
        d["detail"] = self.detail[:300]
        return d


class EvidenceLog:
    """Append-only per-task evidence ledger."""

    def __init__(self, session_dir: Path | None = None) -> None:
        self._entries: list[StepEvidence] = []
        self._session_dir = session_dir
        self._jsonl_path = session_dir / "evidence.jsonl" if session_dir else None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        step: int,
        action: str,
        args: dict[str, Any],
        *,
        ok: bool,
        failure: dict[str, Any] | None,
        detail: str,
        ref: str | None,
        url_before: str | None,
        url_after: str | None,
        attributed_requests: list[dict[str, Any]],
        screenshot: str | None = None,
        data_keys: list[str] | None = None,
        duration_ms: float = 0.0,
    ) -> StepEvidence:
        from urllib.parse import urlparse

        route_changed = False
        try:
            rb = urlparse(url_before or "").path
            ra = urlparse(url_after or "").path
            route_changed = rb != ra and bool(ra)
        except ValueError:
            route_changed = False

        entry = StepEvidence(
            step=step,
            action=action,
            args=dict(args or {}),
            ok=ok,
            failure=failure,
            detail=detail,
            ref=ref,
            url_before=url_before,
            url_after=url_after,
            route_changed=route_changed,
            attributed_requests=attributed_requests,
            screenshot=screenshot,
            data_keys=data_keys or [],
            duration_ms=duration_ms,
        )
        self._entries.append(entry)
        self._append_jsonl(entry)
        return entry

    def _append_jsonl(self, entry: StepEvidence) -> None:
        if self._jsonl_path is None:
            return
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.as_dict(), default=str) + "\n")
        except OSError as e:
            log.debug("evidence jsonl append failed", extra={"event": "evidence.io.fail", "err": str(e)})

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def to_list(self, limit: int = _MAX_EVIDENCE_IN_REPORT) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self._entries[-limit:]]

    def summary(self) -> dict[str, Any]:
        failures = [e for e in self._entries if not e.ok]
        return {
            "steps": len(self._entries),
            "failed_steps": len(failures),
            "route_changes": sum(1 for e in self._entries if e.route_changed),
            "network_events_attributed": sum(len(e.attributed_requests) for e in self._entries),
            "failure_kinds": sorted({e.failure["kind"] for e in failures if e.failure}),
        }

    @property
    def entries(self) -> list[StepEvidence]:
        return list(self._entries)


__all__ = ["EvidenceLog", "StepEvidence"]
