"""Pydantic models: BrowserState, network records, results, API schemas.

`BrowserState` is the single source of truth the agent reasons from.
All records are JSON-serializable so they can be returned via the API and
inspected by SERA without any Playwright knowledge.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


class NetworkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    method: str
    url: str
    resource_type: str | None = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_size: int | None = None
    initiator: str | None = None
    post_data: str | None = None
    timestamp: float = Field(default_factory=time.time)
    # Auth indicators are kept as booleans, never raw values.
    has_authorization: bool = False
    has_cookie: bool = False
    # Response (filled when received).
    status: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_size: int | None = None
    mime_type: str | None = None
    from_cache: bool = False
    redirect_from: str | None = None
    redirect_to: str | None = None
    timing_ms: float | None = None
    graphql: bool = False
    graphql_operation: str | None = None


class WebSocketEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    url: str
    event: Literal["open", "close", "frame_sent", "frame_received", "error"]
    payload_preview: str | None = None
    timestamp: float = Field(default_factory=time.time)


class DownloadRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    suggested_filename: str
    url: str | None = None
    saved_path: str | None = None
    timestamp: float = Field(default_factory=time.time)


class ConsoleMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    text: str
    url: str | None = None
    line: int | None = None
    timestamp: float = Field(default_factory=time.time)


class PageError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: str
    url: str | None = None
    timestamp: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DOM / accessibility snapshot
# ---------------------------------------------------------------------------


class InteractiveElement(BaseModel):
    """An interactive element from the current snapshot.

    ``ref`` is the ElementRegistry's authoritative reference (``e001`` …)
    for elements observed through the live path. Report-only descriptive
    elements (e.g. form fields that could not be matched to a live element)
    may carry ``ref=None`` — the LLM may only act on refs that appear in
    the current element view.
    """

    model_config = ConfigDict(extra="ignore")

    ref: str | None = None
    role: str
    name: str
    tag: str | None = None
    href: str | None = None
    type: str | None = None
    value: str | None = None
    placeholder: str | None = None
    checked: bool | None = None
    disabled: bool = False
    visible: bool = True
    # For forms / inputs
    form_id: str | None = None
    # Playwright-resolved selector (validated against snapshot)
    selector: str | None = None


class FormDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ref: str
    action: str | None = None
    method: str | None = None
    fields: list[InteractiveElement] = Field(default_factory=list)
    has_password: bool = False
    has_csrf: bool = False
    submit_ref: str | None = None


class LinkDescriptor(BaseModel):
    """A discovered link. ``ref`` is the registry ref of the matching
    interactive element when one exists (actionable); None otherwise
    (report-only descriptor)."""

    model_config = ConfigDict(extra="ignore")

    ref: str | None = None
    href: str
    text: str
    internal: bool = True


class AccessibilityNode(BaseModel):
    """Minimal serializable accessibility node (tree-aware)."""
    model_config = ConfigDict(extra="ignore")
    role: str
    name: str
    ref: str | None = None
    children: list["AccessibilityNode"] = Field(default_factory=list)


AccessibilityNode.model_rebuild()


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------


class Challenge(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal[
        "captcha",
        "cloudflare_turnstile",
        "cloudflare_interstitial",
        "hcaptcha",
        "recaptcha",
        "verify_human",
        "bot_detection",
        "login_challenge",
        "mfa",
        "unknown",
    ]
    evidence: str
    selector: str | None = None
    handled: bool = False
    handling: str | None = None  # "official_test_key" | "human_in_loop" | "skipped"
    timestamp: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Browser state
# ---------------------------------------------------------------------------


class Timings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    navigation_ms: float | None = None
    snapshot_ms: float | None = None
    last_action_ms: float | None = None
    total_steps: int = 0


class BrowserState(BaseModel):
    """Structured browser state. Single source of truth per task.

    The agent reads this instead of re-scraping on every step.
    """

    model_config = ConfigDict(extra="ignore")

    task_id: str
    target: str

    # Identity
    current_url: str | None = None
    title: str | None = None
    page_type: str | None = None  # spa | static | login | challenge | error | unknown

    # History
    navigation_history: list[str] = Field(default_factory=list)

    # Auth
    authenticated: bool = False
    user_role: str | None = None
    # v4: evidence-based auth state machine name
    # (ANONYMOUS | LOGIN_ATTEMPT | AUTHENTICATED | LOGIN_FAILED | ...)
    auth_state: str | None = None

    # DOM/acc tree
    snapshot: list[AccessibilityNode] = Field(default_factory=list)
    dom_hash: str | None = None
    interactive_elements: list[InteractiveElement] = Field(default_factory=list)
    forms: list[FormDescriptor] = Field(default_factory=list)
    links: list[LinkDescriptor] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    api_endpoints: list[str] = Field(default_factory=list)

    # Network
    requests: deque[NetworkRequest] = Field(default_factory=lambda: deque(maxlen=500))
    responses: deque[NetworkRequest] = Field(default_factory=lambda: deque(maxlen=500))
    websockets: deque[WebSocketEvent] = Field(default_factory=lambda: deque(maxlen=200))
    downloads: list[DownloadRecord] = Field(default_factory=list)
    console_errors: deque[ConsoleMessage] = Field(default_factory=lambda: deque(maxlen=200))
    page_errors: deque[PageError] = Field(default_factory=lambda: deque(maxlen=100))

    # Storage
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    local_storage_keys: list[str] = Field(default_factory=list)
    session_storage_keys: list[str] = Field(default_factory=list)

    # Screenshots
    screenshots: list[str] = Field(default_factory=list)

    # Challenges
    challenges: list[Challenge] = Field(default_factory=list)

    # Reasoning breadcrumbs
    last_action: str | None = None
    last_observation: str | None = None
    observations: list[str] = Field(default_factory=list)

    # Timings
    timings: Timings = Field(default_factory=Timings)

    # Misc
    technologies: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

    def add_observation(self, text: str) -> None:
        self.observations.append(text)
        self.last_observation = text

    def recompute_dom_hash(self, raw: str) -> str:
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()  # nosec: B324 (not crypto)
        self.dom_hash = h
        return h

    def to_api_dict(self, *, with_snapshot: bool = True) -> dict[str, Any]:
        """JSON-safe view for the API. deque → list, with truncation."""
        data = self.model_dump(mode="json")
        data["requests"] = list(data["requests"])[-200:]
        data["responses"] = list(data["responses"])[-200:]
        data["websockets"] = list(data["websockets"])
        data["console_errors"] = list(data["console_errors"])
        data["page_errors"] = list(data["page_errors"])
        if not with_snapshot:
            data["snapshot"] = []
        return data


# ---------------------------------------------------------------------------
# Result / report
# ---------------------------------------------------------------------------


class AuthenticationSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    detected: bool = False
    type: str | None = None  # form | bearer | cookie | token | none
    login_url: str | None = None
    roles_observed: list[str] = Field(default_factory=list)
    storage_state_saved: bool = False
    csrf_observed: bool = False


class NetworkSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_count: int = 0
    response_count: int = 0
    api_endpoints: list[str] = Field(default_factory=list)
    graphql_operations: list[str] = Field(default_factory=list)
    websocket_endpoints: list[str] = Field(default_factory=list)
    auth_indicators: list[str] = Field(default_factory=list)
    redirects: list[tuple[str, str, int]] = Field(default_factory=list)
    status_breakdown: dict[str, int] = Field(default_factory=dict)


class AgentReport(BaseModel):
    """Deterministic structured report returned to SERA.

    v4 additive fields (coverage / evidence / app_graph / auth_states /
    blocked_targets / intelligence) extend the report WITHOUT removing or
    renaming any v3 field — the SERA persistence contract (report keys)
    is preserved. Consumers that ignore unknown keys are unaffected.
    """

    model_config = ConfigDict(extra="ignore")
    status: Literal[
        "completed", "cancelled", "failed", "blocked", "timeout"
    ]
    target: str
    title: str | None = None
    technology: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    apis: list[str] = Field(default_factory=list)
    forms: list[dict[str, Any]] = Field(default_factory=list)
    authentication: AuthenticationSummary = Field(default_factory=AuthenticationSummary)
    workflows: list[str] = Field(default_factory=list)
    network: NetworkSummary = Field(default_factory=NetworkSummary)
    screenshots: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    challenges: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    summary: str = ""
    task_id: str
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: float | None = None
    steps: int = 0
    browser_mode: str | None = None
    error: str | None = None
    # --- v4 additive fields -------------------------------------------
    # Machine-readable coverage metrics (routes/elements/forms/endpoints/
    # workflows/auth-states: discovered vs explored vs blocked).
    coverage: dict[str, Any] | None = None
    # Per-step structured evidence (action, result, network, screenshots).
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # Application graph: UI actions correlated with routes + endpoints.
    app_graph: dict[str, Any] | None = None
    # Evidence-based auth state machine summary.
    auth_states: dict[str, Any] | None = None
    # Targets abandoned by loop escape / failure blocking.
    blocked_targets: list[str] = Field(default_factory=list)
    # Element-registry-level stats (ref namespace integrity).
    registry_stats: dict[str, Any] | None = None
    # Backend used (local-playwright | kernel).
    browser_backend: str | None = None


# ---------------------------------------------------------------------------
# API request schema
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: str
    task: str
    browser_mode: Literal["auto", "headed", "headless"] | None = None
    timeout: int | None = None
    local_lab: bool = False
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    auth: dict[str, Any] | None = None  # opaque; storage_state path / cookies / headers
    max_steps: int | None = None
    allow_js_eval: bool | None = None


class TaskCreatedResponse(BaseModel):
    task_id: str
    status: str
    target: str
