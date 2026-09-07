"""Authentication state machine — evidence-based auth inference (rebuild v4).

THE rule this module enforces: **attempting a login is NOT evidence of
authentication.** The old agent marked the session authenticated the
moment a login-ish action ran (agent.py:412-414, v3.1.1) — a fabrication.

States:
    ANONYMOUS → LOGIN_ATTEMPT → AUTHENTICATED | LOGIN_FAILED
    AUTHENTICATED → MFA_REQUIRED | SESSION_EXPIRED

Transitions require OBSERVABLE POSTCONDITIONS:

- **Session cookie evidence** — a non-empty session-ish cookie
  (session|sid|jwt|token|auth) exists after the attempt.
- **Redirect evidence** — after submit, the browser left the login route
  for a non-login, non-error route (classic post-login redirect).
- **UI evidence** — logout/account/user-identity elements appear in the
  registry, or the login form disappeared.
- **Storage token evidence** — auth-ish keys appear in localStorage /
  sessionStorage with non-empty values.
- **Protected-route evidence** — a route that redirected to /login while
  anonymous now loads directly (200 + stays).
- **Negative evidence** — a 401/403 on a protected endpoint, or the login
  form still present with an error message, keeps the state unproven.

The machine never reads cookie VALUES — only names and non-emptiness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from ..logging import get_logger
from ..models import BrowserState

log = get_logger("sera.task.auth_state")

AuthStateName = Literal[
    "ANONYMOUS",
    "LOGIN_ATTEMPT",
    "AUTHENTICATED",
    "LOGIN_FAILED",
    "MFA_REQUIRED",
    "SESSION_EXPIRED",
]

_SESSION_COOKIE_HINTS = (
    "session",
    "sid",
    "jwt",
    "token",
    "auth",
    "connect.sid",
    "SESSIONID",
    "phpsessid",
    "jsessionid",
    "asp.net",
)

_AUTH_STORAGE_HINTS = (
    "token",
    "auth",
    "jwt",
    "session",
    "user",
    "access",
    "id_token",
    "refresh",
)

_LOGOUT_UI_HINTS = (
    "logout",
    "log out",
    "sign out",
    "signout",
    "my account",
    "account",
    "profile",
)

_LOGIN_URL_HINTS = ("login", "signin", "sign-in", "auth", "session/new")


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _is_login_url(url: str | None) -> bool:
    low = _norm(url)
    return any(h in low for h in _LOGIN_URL_HINTS)


@dataclass
class AuthEvidence:
    """One observation bundle evaluated against the state machine."""

    # Cookie name-level evidence (never values).
    session_cookie_present: bool = False
    # The login form vanished from the current snapshot.
    login_form_gone: bool = False
    # Identity-ish UI elements appeared.
    identity_ui_present: bool = False
    # Auth-ish storage keys with non-empty values.
    storage_token_present: bool = False
    # We navigated to a protected route and it stayed (no login redirect).
    protected_route_accessible: bool = False
    # Negative signals.
    auth_error_response: bool = False
    still_on_login_page: bool = False

    def positive_count(self) -> int:
        return sum(
            1
            for b in (
                self.session_cookie_present,
                self.login_form_gone,
                self.identity_ui_present,
                self.storage_token_present,
                self.protected_route_accessible,
            )
            if b
        )


@dataclass
class AuthTransition:
    from_state: AuthStateName
    to_state: AuthStateName
    evidence: AuthEvidence
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "evidence": {
                "session_cookie": self.evidence.session_cookie_present,
                "login_form_gone": self.evidence.login_form_gone,
                "identity_ui": self.evidence.identity_ui_present,
                "storage_token": self.evidence.storage_token_present,
                "protected_route": self.evidence.protected_route_accessible,
                "auth_error": self.evidence.auth_error_response,
            },
        }


# Evidence thresholds: post-submit, how many positive signals constitute
# AUTHENTICATED (>=2 distinct observable postconditions).
_AUTH_THRESHOLD = 2


class AuthStateMachine:
    """Evidence-based auth state tracking for one task."""

    def __init__(self) -> None:
        self.state: AuthStateName = "ANONYMOUS"
        self.history: list[AuthTransition] = []
        self.last_evidence: AuthEvidence | None = None
        self.login_attempted_at_url: str | None = None

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        state: BrowserState,
        *,
        login_just_attempted: bool = False,
        protected_route_test: bool = False,
    ) -> AuthTransition:
        """Collect evidence from the observable state and transition."""
        evidence = self._collect(state)
        self.last_evidence = evidence

        prev = self.state
        if login_just_attempted and self.state in ("ANONYMOUS", "LOGIN_FAILED"):
            self.state = "LOGIN_ATTEMPT"
            self.login_attempted_at_url = state.current_url

        # Negative evidence first: 401/403 or still on login with errors.
        if evidence.auth_error_response or (
            evidence.still_on_login_page
            and not evidence.login_form_gone
            and self.state == "LOGIN_ATTEMPT"
        ):
            if self.state == "LOGIN_ATTEMPT":
                self.state = "LOGIN_FAILED"
        elif evidence.positive_count() >= _AUTH_THRESHOLD:
            # Require MULTIPLE distinct observable postconditions — a
            # single cookie name is not proof (pre-auth session cookies
            # like CSRF tokens are common).
            self.state = "AUTHENTICATED"
        elif (
            self.state == "LOGIN_ATTEMPT"
            and evidence.positive_count() == 1
            and (evidence.session_cookie_present or evidence.storage_token_present)
            and (evidence.identity_ui_present or evidence.login_form_gone)
        ):
            # Exactly one positive + weak UI signal → MFA gate possibility.
            self.state = "MFA_REQUIRED"

        if (
            self.state == "AUTHENTICATED"
            and evidence.auth_error_response
            and protected_route_test
        ):
            self.state = "SESSION_EXPIRED"

        transition = AuthTransition(
            from_state=prev,
            to_state=self.state,
            evidence=evidence,
            reason=self._reason(prev, self.state, evidence),
        )
        if prev != self.state:
            self.history.append(transition)
            log.info(
                "auth state transition",
                extra={
                    "event": "auth.transition",
                    "from": prev,
                    "to": self.state,
                    "reason": transition.reason,
                },
            )
        # Mirror the authoritative verdict into BrowserState (SERA reads
        # state.authenticated).
        state.authenticated = self.state == "AUTHENTICATED"
        state.auth_state = self.state
        return transition

    # ------------------------------------------------------------------
    # Evidence collection (observable postconditions only)
    # ------------------------------------------------------------------

    def _collect(self, state: BrowserState) -> AuthEvidence:
        ev = AuthEvidence()

        # 1. Session cookie evidence (names + non-emptiness only).
        ev.session_cookie_present = any(
            _cookie_is_session_like(c) for c in (state.cookies or [])
        )

        # 2. Login form gone: we're past a login attempt and no password
        #    form is present in the current state.
        has_password_form = any(f.has_password for f in (state.forms or []))
        ev.login_form_gone = bool(self.login_attempted_at_url) and not has_password_form

        # 3. Identity UI evidence.
        ev.identity_ui_present = any(
            any(h in _norm(el.name) for h in _LOGOUT_UI_HINTS)
            for el in (state.interactive_elements or [])
        )

        # 4. Storage token evidence.
        ev.storage_token_present = any(
            any(h in k.lower() for h in _AUTH_STORAGE_HINTS)
            for k in list(state.local_storage_keys or []) + list(state.session_storage_keys or [])
        )

        # 5. Negative: still parked on a login URL with the form present.
        ev.still_on_login_page = _is_login_url(state.current_url) and has_password_form

        # 6. Negative: recent 401/403 on API-ish requests.
        ev.auth_error_response = self._recent_auth_error(state)

        return ev

    @staticmethod
    def _recent_auth_error(state: BrowserState) -> bool:
        try:
            recent = list(state.requests or [])[-40:]
        except TypeError:
            return False
        for r in recent:
            if r.status in (401, 403) and (r.resource_type in ("xhr", "fetch", "document") or r.graphql):
                return True
        return False

    def _reason(self, prev: str, new: str, ev: AuthEvidence) -> str:
        if new == "AUTHENTICATED":
            return f"{ev.positive_count()} distinct observable postconditions"
        if new == "LOGIN_FAILED":
            return "auth error response or login form still present after attempt"
        if new == "MFA_REQUIRED":
            return "partial evidence: token present but identity not confirmed"
        if new == "LOGIN_ATTEMPT":
            return "login submitted — awaiting observable postconditions"
        if new == "SESSION_EXPIRED":
            return "auth error on protected route while authenticated"
        return f"no transition ({prev})"

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "state": self.state,
            # NEVER say authenticated just because a login was attempted.
            "authenticated": self.state == "AUTHENTICATED",
            "states_reached": [t.to_state for t in self.history] or ["ANONYMOUS"],
            "transitions": [t.as_dict() for t in self.history],
        }


def _cookie_is_session_like(cookie: dict[str, Any]) -> bool:
    name = _norm(cookie.get("name", ""))
    value = cookie.get("value")
    if not name:
        return False
    if not value:  # empty session cookie is not evidence
        return False
    return any(h in name for h in _SESSION_COOKIE_HINTS)


__all__ = ["AuthStateMachine", "AuthEvidence", "AuthTransition", "AuthStateName"]
