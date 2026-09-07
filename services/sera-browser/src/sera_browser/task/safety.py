"""Deterministic, code-enforced safety boundaries.

Inspired by the survey §8.4 and Playwright MCP's `--secrets`,
`--allowed-origins`/`--blocked-origins`. These boundaries are enforced in
CODE, not in the LLM prompt — prompt injection via page content can hijack
a general-purpose agent, so the execution layer must be the last line of
defense.

Boundaries:
1. **Sensitive-keyword action confirmation** — actions that would perform
   irreversible operations (delete, transfer, refund, password change,
   purchase) require explicit confirmation via `confirm_dangerous_action()`.
   In autonomous mode, these are BLOCKED by default.
2. **Action allowlist per specialized agent** — the agent can be configured
   with a restricted action set (e.g. "read-only" = navigate/click/scroll/
   screenshot only, no fill/form-submit).
3. **Secrets redaction in tool responses** — the agent never puts raw
   credentials in the LLM context. Storage state, cookies, and auth headers
   are redacted before they reach the state view.
4. **Domain allow/block lists** — beyond SSRF protection, the agent can
   refuse to navigate to domains on a blocklist even if they're public.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..logging import get_logger
from ..models import BrowserState, InteractiveElement

log = get_logger("sera.task.safety")


# Keywords that signal an irreversible/dangerous action. If the LLM's chosen
# action targets an element whose name/text matches any of these, we require
# confirmation (or block in autonomous mode).
_DANGEROUS_KEYWORDS = {
    "delete",
    "remove",
    "destroy",
    "drop",
    "transfer",
    "send money",
    "wire",
    "refund",
    "charge",
    "purchase",
    "buy",
    "checkout",
    "submit payment",
    "confirm order",
    "place order",
    "sign out",  # ending a session is often irreversible in test contexts
    "logout",
    "revoke",
    "disable",
    "terminate",
    "cancel subscription",
    "unsubscribe",
    "reset password",
    "change password",
    "grant access",
    "revoke access",
    "make admin",
    "promote",
    "demote",
    "fire",
    "approve",
    "reject",
    "publish",
    "deploy",
    "merge",
    "format",
}


# Read-only action set — no mutations.
READONLY_ACTIONS = frozenset({
    "navigate",
    "click",  # clicking is allowed but dangerous clicks are still gated
    "hover",
    "scroll",
    "screenshot",
    "finish",
    "blocked",
    "noop",
    "back",
    "forward",
    "reload",
    "extract",
    "search_page",
    "find_elements",
})

# Full action set — all mutations allowed (subject to dangerous-keyword gating).
FULL_ACTIONS = READONLY_ACTIONS | frozenset({
    "fill",
    "fill_form",
    "select",
    "check",
    "uncheck",
    "press",
    "mouse_click",
    "switch_tab",
    "close_tab",
    "new_tab",
    "save_pdf",
    "download",
    "upload_file",
    "drag",
    "evaluate",
})


ActionPolicy = Literal["readonly", "full", "custom"]


@dataclass
class SafetyConfig:
    """Per-task safety configuration."""
    # Action policy: "readonly" blocks all mutations, "full" allows all,
    # "custom" uses `allowed_actions` as the allowlist.
    action_policy: ActionPolicy = "full"
    allowed_actions: frozenset[str] = field(default_factory=lambda: FULL_ACTIONS)
    # If True, dangerous-keyword actions are blocked entirely (autonomous mode).
    # If False, they're allowed but logged at WARNING level (supervised mode).
    block_dangerous: bool = True
    # Additional dangerous keywords for this task.
    extra_dangerous_keywords: set[str] = field(default_factory=set)
    # Domains the agent must NEVER navigate to (blocklist).
    blocked_domains: set[str] = field(default_factory=set)


@dataclass
class SafetyDecision:
    """The outcome of a safety check."""
    allowed: bool
    reason: str = ""
    is_dangerous: bool = False
    matched_keyword: str | None = None
    requires_confirmation: bool = False


class SafetyGuard:
    """Enforces safety boundaries in code, not in the LLM prompt."""

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self.config = config or SafetyConfig()
        self._dangerous_keywords = _DANGEROUS_KEYWORDS | {
            k.lower() for k in self.config.extra_dangerous_keywords
        }

    def check_action(
        self,
        action: str,
        args: dict[str, Any],
        state: BrowserState,
    ) -> SafetyDecision:
        """Check whether an action is allowed by the safety policy.

        Returns a SafetyDecision. If `allowed` is False, the caller MUST
        refuse to execute the action.
        """
        # 1. Action allowlist
        if not self._is_action_allowed(action):
            return SafetyDecision(
                allowed=False,
                reason=f"action {action!r} not in allowlist (policy={self.config.action_policy})",
            )

        # 2. Navigate domain blocklist
        if action == "navigate":
            url = args.get("url", "")
            blocked = self._check_domain_blocklist(url)
            if blocked is not None:
                return SafetyDecision(
                    allowed=False,
                    reason=f"navigate to blocked domain: {blocked}",
                )

        # 3. Dangerous-keyword detection for ref-based actions
        if action in {"click", "fill", "fill_form", "select", "check", "uncheck", "hover"}:
            element_text = self._extract_element_text(action, args, state)
            if element_text:
                matched = self._find_dangerous_keyword(element_text)
                if matched:
                    if self.config.block_dangerous:
                        return SafetyDecision(
                            allowed=False,
                            is_dangerous=True,
                            matched_keyword=matched,
                            requires_confirmation=True,
                            reason=f"dangerous keyword {matched!r} in element text; "
                            f"blocked (block_dangerous=True)",
                        )
                    log.warning(
                        "dangerous action allowed (supervised mode)",
                        extra={
                            "event": "safety.dangerous.allowed",
                            "action": action,
                            "keyword": matched,
                            "element_text": element_text[:100],
                        },
                    )
                    return SafetyDecision(
                        allowed=True,
                        is_dangerous=True,
                        matched_keyword=matched,
                        requires_confirmation=True,
                        reason=f"dangerous keyword {matched!r} (supervised mode)",
                    )

        # 4. evaluate (JS) is always dangerous — only allowed if explicitly enabled
        if action == "evaluate":
            if not self._is_action_allowed("evaluate"):
                return SafetyDecision(
                    allowed=False,
                    reason="JS evaluation not in allowlist",
                )

        return SafetyDecision(allowed=True)

    def _is_action_allowed(self, action: str) -> bool:
        if self.config.action_policy == "readonly":
            return action in READONLY_ACTIONS
        if self.config.action_policy == "full":
            return action in FULL_ACTIONS
        return action in self.config.allowed_actions

    def _check_domain_blocklist(self, url: str) -> str | None:
        if not url or not self.config.blocked_domains:
            return None
        try:
            from urllib.parse import urlparse

            host = urlparse(url).netloc.lower()
            for blocked in self.config.blocked_domains:
                if blocked.lower() in host:
                    return blocked
        except Exception:  # noqa: BLE001
            pass
        return None

    def _extract_element_text(
        self,
        action: str,
        args: dict[str, Any],
        state: BrowserState,
    ) -> str:
        """Extract the text/name of the element the action targets."""
        ref = args.get("ref")
        if not ref or not state.interactive_elements:
            return ""
        # Find the element by ref (try integer index, then old format).
        for el in state.interactive_elements:
            if el.ref == ref or el.ref.endswith(f":{ref}"):
                return f"{el.name} {el.placeholder or ''}".lower()
        return ""

    def _find_dangerous_keyword(self, text: str) -> str | None:
        text_lower = text.lower()
        for kw in self._dangerous_keywords:
            if kw in text_lower:
                return kw
        return None


def redact_sensitive_in_state(state: BrowserState) -> dict[str, Any]:
    """Redact sensitive values before sending state to the LLM.

    Returns a dict suitable for the LLM state view. Cookies values are
    masked; localStorage keys are listed but values are not included;
    auth headers in network requests are already redacted by NetworkRecorder.
    """
    view: dict[str, Any] = {
        "cookie_count": len(state.cookies),
        "cookie_names": [c.get("name", "") for c in state.cookies],
        "local_storage_keys": list(state.local_storage_keys),
        "session_storage_keys": list(state.session_storage_keys),
        "authenticated": state.authenticated,
        "user_role": state.user_role,
    }
    return view


__all__ = [
    "SafetyConfig",
    "SafetyDecision",
    "SafetyGuard",
    "READONLY_ACTIONS",
    "FULL_ACTIONS",
    "redact_sensitive_in_state",
]
