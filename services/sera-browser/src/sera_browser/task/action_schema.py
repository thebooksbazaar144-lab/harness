"""Action schema + validator — the strict contract for LLM-chosen actions.

The LLM is NEVER allowed to directly execute arbitrary Playwright code.
It returns a single JSON object describing the action it wants the
ActionExecutor to perform. We validate that object strictly against the
current BrowserState before handing it to the executor:

- action name must be in the allowed set
- args must match the schema for that action
- ref-based actions must reference an element present in
  `state.interactive_elements`
- navigate must pass SSRF validation
- navigate that targets the current URL is allowed (idempotent) but flagged
- fill/select values must be strings

If validation fails, the caller (LLM planner) falls back to the deterministic
planner for that step.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from ..logging import get_logger
from ..models import BrowserState
from ..security import TargetValidationError, validate_target
from ..config import Settings
from .planner import Step

log = get_logger("sera.task.action_schema")


# The complete set of actions the LLM may request. Anything else is rejected.
AllowedAction = Literal[
    # Navigation
    "navigate",
    "back",
    "forward",
    "reload",
    # Element interaction
    "click",
    "fill",
    "fill_form",  # bulk: multiple fields at once
    "select",
    "check",
    "uncheck",
    "hover",
    "scroll",
    "press",
    "mouse_click",  # coordinate-based (vision fallback)
    # Tabs
    "switch_tab",
    "close_tab",
    "new_tab",
    # Capture / extraction
    "screenshot",
    "extract",  # LLM-based structured extraction
    "scrape",  # CSS-based structured extraction (dsh-browser)
    "search_page",  # regex/text search over the page
    "find_elements",  # find by CSS/role without acting
    "content",  # multi-format page content fetch (dsh-browser)
    # File ops
    "upload_file",
    "save_pdf",
    "download",
    # Auth (dsh-browser)
    "flush_auth",  # export cookies
    "restore_auth",  # import cookies
    # History / Replay (dsh-browser)
    "history",  # list recorded operations
    "replay",  # replay a recorded operation
    # Soft restrict (dsh-browser)
    "restrict",  # set/lift action allowlist
    # Advanced
    "drag",
    "evaluate",  # JS eval (gated, dangerous)
    # Lifecycle
    "finish",
    "blocked",
    "update_memory",  # update long_term_memory field
]


# Per-action required + optional args. Keys not listed here are rejected.
_ACTION_ARG_SPEC: dict[str, dict[str, type]] = {
    # Navigation
    "navigate": {"url": str},
    "back": {},
    "forward": {},
    "reload": {},
    # Element interaction
    "click": {"ref": str},
    "fill": {"ref": str, "value": str},
    "fill_form": {"fields": list},  # list of {ref, value}
    "select": {"ref": str, "value": str},
    "check": {"ref": str},
    "uncheck": {"ref": str},
    "hover": {"ref": str},
    "scroll": {},  # selector/dy/into_element optional
    "press": {"key": str},
    "mouse_click": {"x": float, "y": float},  # accepts int or float
    # Tabs
    "switch_tab": {"tab_id": str},
    "close_tab": {"tab_id": str},
    "new_tab": {"url": str},
    # Capture / extraction
    "screenshot": {},  # label optional
    "extract": {"prompt": str},  # schema optional
    "scrape": {"item": str, "fields": list},  # dsh-browser: CSS-based extraction
    "search_page": {"pattern": str},
    "find_elements": {"selector": str},
    "content": {"format": str},  # dsh-browser: html/markdown/txt/json
    # File ops
    "upload_file": {"ref": str, "path": str},
    "save_pdf": {},
    "download": {"url": str},
    # Auth (dsh-browser)
    "flush_auth": {},  # no args; exports cookies
    "restore_auth": {"cookies": list},  # imports cookies
    # History / Replay (dsh-browser)
    "history": {},  # no args; lists entries
    "replay": {"seq": int},  # replay by sequence number
    # Soft restrict (dsh-browser)
    "restrict": {"allowed": list},  # set/lift allowlist
    # Advanced
    "drag": {"start_ref": str, "end_ref": str},
    "evaluate": {"script": str},
    # Lifecycle
    "finish": {},
    "blocked": {},
    "update_memory": {"memory": str},
}

_OPTIONAL_ARGS: dict[str, dict[str, type]] = {
    "navigate": {"force": bool},
    "scroll": {"selector": str, "dy": int, "into_ref": str},
    "screenshot": {"label": str},
    "extract": {"schema": dict},
    "find_elements": {"role": str, "name": str},
    "new_tab": {"active": bool},
    "save_pdf": {"path": str},
    "finish": {"reason": str, "memory": str},
    "blocked": {"reason": str},
    "mouse_click": {"button": str},
    "content": {"selector": str, "max_chars": int},  # dsh-browser
}


@dataclass
class ValidationResult:
    ok: bool
    step: Step | None
    reason: str = ""


class ActionValidationError(Exception):
    """Raised when the LLM-returned action does not pass validation."""


def parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse the LLM's JSON response.

    The LLM is instructed to return strict JSON. We tolerate:
    - JSON wrapped in markdown code fences ```json ... ```
    - Leading/trailing whitespace
    - A leading "thinking:" line we strip

    Returns the parsed dict, or raises ActionValidationError.
    """
    if not raw or not raw.strip():
        raise ActionValidationError("empty LLM response")

    text = raw.strip()

    # Strip markdown code fences if present.
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        # Remove closing fence
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    # Sometimes the model emits a single line of "thinking" before JSON.
    # Try direct parse first; if it fails, find the first '{' and last '}'.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Best-effort: find the first '{' and the matching last '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            raise ActionValidationError(f"LLM response is not valid JSON: {e}")

    raise ActionValidationError(f"LLM response contains no JSON object: {raw[:120]!r}")


def validate_action(
    parsed: dict[str, Any],
    state: BrowserState,
    settings: Settings,
) -> ValidationResult:
    """Validate a parsed LLM action against the current BrowserState.

    Returns a ValidationResult. If `ok` is False, the caller should fall
    back to the deterministic planner.
    """
    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, step=None, reason="response is not a JSON object")

    action = parsed.get("action")
    if not isinstance(action, str):
        return ValidationResult(ok=False, step=None, reason="missing or non-string 'action'")

    if action not in _ACTION_ARG_SPEC:
        return ValidationResult(
            ok=False, step=None, reason=f"unknown action: {action!r}"
        )

    args = parsed.get("args") or {}
    if not isinstance(args, dict):
        return ValidationResult(
            ok=False, step=None, reason="'args' must be a JSON object"
        )

    # Check required args are present and correctly typed.
    spec = _ACTION_ARG_SPEC[action]
    for arg_name, arg_type in spec.items():
        if arg_name not in args:
            return ValidationResult(
                ok=False, step=None, reason=f"action {action!r} missing required arg {arg_name!r}"
            )
        if not _is_type(args[arg_name], arg_type):
            return ValidationResult(
                ok=False,
                step=None,
                reason=f"action {action!r} arg {arg_name!r} must be {arg_type.__name__}, got {type(args[arg_name]).__name__}",
            )

    # Check optional args are correctly typed if present.
    opt = _OPTIONAL_ARGS.get(action, {})
    for arg_name, arg_type in opt.items():
        if arg_name in args and not _is_type(args[arg_name], arg_type):
            return ValidationResult(
                ok=False,
                step=None,
                reason=f"action {action!r} arg {arg_name!r} must be {arg_type.__name__}",
            )

    # Reject unknown args (defense in depth).
    allowed_keys = set(spec.keys()) | set(opt.keys())
    extra = set(args.keys()) - allowed_keys
    if extra:
        return ValidationResult(
            ok=False,
            step=None,
            reason=f"action {action!r} has unexpected args: {sorted(extra)}",
        )

    # Ref-based actions: the ref MUST exist in the current snapshot.
    if "ref" in spec:
        ref = args["ref"]
        if not _ref_exists(ref, state):
            return ValidationResult(
                ok=False,
                step=None,
                reason=f"ref {ref!r} not present in current interactive_elements",
            )

    # fill_form: validate each {ref, value} entry.
    if action == "fill_form":
        fields = args.get("fields")
        if not isinstance(fields, list) or not fields:
            return ValidationResult(
                ok=False, step=None, reason="fill_form requires a non-empty 'fields' list"
            )
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                return ValidationResult(
                    ok=False, step=None, reason=f"fill_form field {i} must be an object"
                )
            ref = f.get("ref")
            value = f.get("value")
            if not isinstance(ref, str) or not ref:
                return ValidationResult(
                    ok=False, step=None, reason=f"fill_form field {i} missing 'ref'"
                )
            if not isinstance(value, str):
                return ValidationResult(
                    ok=False, step=None, reason=f"fill_form field {i} 'value' must be string"
                )
            if not _ref_exists(ref, state):
                return ValidationResult(
                    ok=False,
                    step=None,
                    reason=f"fill_form field {i} ref {ref!r} not in interactive_elements",
                )

    # mouse_click: x and y can be int or float.
    if action == "mouse_click":
        for coord in ("x", "y"):
            val = args.get(coord)
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return ValidationResult(
                    ok=False, step=None, reason=f"mouse_click {coord!r} must be a number"
                )

    # drag: both refs must exist.
    if action == "drag":
        for ref_key in ("start_ref", "end_ref"):
            ref = args[ref_key]
            if not _ref_exists(ref, state):
                return ValidationResult(
                    ok=False,
                    step=None,
                    reason=f"drag {ref_key} {ref!r} not in interactive_elements",
                )

    # upload_file: ref must exist.
    if action == "upload_file":
        ref = args["ref"]
        if not _ref_exists(ref, state):
            return ValidationResult(
                ok=False, step=None, reason=f"upload_file ref {ref!r} not in interactive_elements"
            )

    # navigate: SSRF + allowlist validation.
    if action == "navigate":
        url = args["url"]
        if not isinstance(url, str) or not url:
            return ValidationResult(ok=False, step=None, reason="navigate url empty")
        try:
            validate_target(url, settings)
        except TargetValidationError as e:
            return ValidationResult(
                ok=False,
                step=None,
                reason=f"navigate target rejected: {e}",
            )

    # new_tab / download: validate the URL too.
    if action in ("new_tab", "download"):
        url = args.get("url", "")
        if url:
            try:
                validate_target(url, settings)
            except TargetValidationError as e:
                return ValidationResult(
                    ok=False, step=None, reason=f"{action} target rejected: {e}"
                )

    # Map to Step. The Step's `kind` carries planner-internal semantics.
    kind = _action_to_kind(action, state)
    step = Step(
        kind=kind,
        action=action,
        args=dict(args),
        reason=str(parsed.get("reason") or parsed.get("thinking") or f"LLM: {action}"),
    )
    return ValidationResult(ok=True, step=step, reason="ok")


def _is_type(value: Any, expected: type) -> bool:
    # bool is a subclass of int; we want strict typing.
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        # Reject bools masquerading as ints.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        # Accept ints as floats (int is a valid float).
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _ref_exists(ref: str, state: BrowserState) -> bool:
    return any(e.ref == ref for e in state.interactive_elements)


def _action_to_kind(action: str, state: BrowserState) -> str:
    """Map an LLM action to a StepKind the orchestrator understands.

    The orchestrator treats `finish` and `blocked` specially (they end the
    loop). Everything else is executed via ActionExecutor.
    """
    if action == "finish":
        return "finish"
    if action == "blocked":
        return "blocked"
    if action == "screenshot":
        return "capture_screenshot"
    if action == "navigate":
        return "navigate"
    return "act"


__all__ = [
    "AllowedAction",
    "ActionValidationError",
    "ValidationResult",
    "parse_llm_response",
    "validate_action",
]
