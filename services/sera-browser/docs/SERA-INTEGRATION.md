# SERA Integration Contract — sera-browser v4 (browser-intelligence rebuild)

This document defines how SERA consumes `sera-browser` v4. SERA uses ONLY
the public API — never internal module paths. The v4 rebuild is
**contract-preserving**: every symbol SERA imports today keeps working,
and the report schema only gains additive sections.

> Companion document: `docs/SERA_REQUIRED_CHANGES.md` — the exact
> implementation contract for the SERA-side coding agent (dispatcher,
> specialists, data model, UI). SERA's repository is read-only for the
> browser-intelligence rebuild; SERA-side changes are made by a separate
> agent against that contract.

---

## 1. Installation

```bash
# From PyPI:
pip install sera-browser

# Optional extras:
pip install "sera-browser[vision]"   # Set-of-Mark vision escalation

# Development install:
pip install -e ".[dev]"
```

## 2. The SERA import surface (hard compatibility contract)

These are the only symbols SERA may import. All of them are stable across
the v3 → v4 rebuild:

```python
from sera_browser import (
    BrowserManager,          # launch / contexts (Playwright lifecycle)
    ChallengeDetector,       # captcha/turnstile detection
    ContextManager,          # isolated browser context per session
    Inspector,               # observe(page) -> SnapshotSummary
    NetworkRecorder,         # structured traffic sensor
    PlaywrightDriver,        # bounded Playwright primitives
    SessionManager,          # task lifecycle (create/cancel/status/report)
    Settings,                # env-driven config (SBA_*)
    TaskCreate,              # task request schema
)
from sera_browser.cancel import CancellationManager
from sera_browser import ElementResolver  # multi-strategy resolution (compat)
```

### Construction signatures (unchanged)

```python
settings = Settings(local_lab=True, allowed_private_targets="127.0.0.1")
bm = BrowserManager(settings)
handle = await bm.launch(mode="headless")
cm = await bm.new_context_manager(cancellation)
page = await cm.open(handle.browser)

driver = PlaywrightDriver(settings=settings, cancellation=cancellation)
inspector = Inspector(driver=driver)            # keyword `driver=` — unchanged
resolver = ElementResolver(page=page)           # keyword `page=` — unchanged
recorder = NetworkRecorder(settings=settings, cancellation=cancellation)
recorder.attach(page)
challenge_detector = ChallengeDetector()
```

### What changed INSIDE these objects (behavior, not API)

| Surface | v3 behavior | v4 behavior |
|---|---|---|
| `Inspector.observe(page).elements[*].ref` | `button#3:Login` (fabricated, 3 competing namespaces) | **`e007`** — the single authoritative ElementRegistry namespace |
| `Inspector.observe(page).forms[*].fields[*].ref` | `form1_text_email` (never actionable) | live registry ref when matched, else `None` |
| `Inspector.observe(page).forms[*].submit_ref` | `button_submit_1` (synthesized, never resolvable) | live registry ref or `None` |
| `Inspector.observe(page).links[*].ref` | `link7` (fabricated) | live registry ref or `None` |
| `PlaywrightDriver.click_ref(...)` | ~15s selector path + ~15s role/name fallback ≈ 32s stuck | ONE bounded budget (default 8s) + ONE tight retry (3s) |
| `NetworkRecorder.requests` | same deque | same deque (unchanged) |
| Snapshot summary fields | url/title/dom_hash/elements/tree/forms/links/routes/snapshot_ms/raw | same + `snapshot_version`, `new_refs`, `lost_refs` (additive) |

SERA code that reads `summary.links` / `summary.forms` / `summary.elements`
keeps working. Any SERA code that pattern-matched the OLD ref formats
(`button#…`, `link\d+`, `form\d+_…`) must switch to the registry refs —
see SERA_REQUIRED_CHANGES.md §3.

## 3. Task lifecycle (SessionManager — unchanged)

```python
from sera_browser import SessionManager, TaskCreate

sm = SessionManager(settings=settings, browser_manager=bm)
rec = await sm.create(TaskCreate(
    target="http://127.0.0.1:3000/",
    task="Analyze this application's routes, forms, APIs and auth surface.",
    browser_mode="headless",
    timeout=180,
    local_lab=True,
    allowed_targets=["127.0.0.1"],
    auth={                      # OPTIONAL: credentials for the single
        "username": "admin",    # evidence-verified login attempt
        "password": "…",
    },
))
# rec.task is an asyncio Task; rec.report is the AgentReport when done.
```

`TaskCreate.auth` with `username`/`password` keys now drives ONE login
attempt whose result is decided by the auth state machine from observable
postconditions (session cookie + redirect + identity UI + storage token).
Attempting a login is never itself treated as authentication.

## 4. AgentReport schema (additive v4 sections)

All v3 fields are unchanged (status, target, title, technology, routes,
apis, forms, authentication, workflows, network, screenshots,
console_errors, challenges, observations, summary, task_id, started_at,
finished_at, duration_ms, steps, browser_mode, error). The following
sections are ADDED — consumers ignoring unknown keys are unaffected:

```jsonc
{
  // ... all v3 fields unchanged ...

  "coverage": {                       // measurable exploration completeness
    "routes":    {"discovered": 12, "visited": 9, "blocked": 1, "percent": 75.0},
    "elements":  {"discovered": 140, "interacted": 38, "blocked": 4, "percent": 27.1},
    "forms":     {"discovered": 3, "inspected": 3, "percent": 100.0},
    "endpoints": {"discovered": 27},
    "workflows": {"discovered": 8},
    "auth_states": {"discovered": ["ANONYMOUS", "LOGIN_ATTEMPT"], "reached": ["ANONYMOUS"]},
    "visited_route_list": ["/", "/login", "/search", "..."],
    "blocked_refs": ["e017"],
    "blocked_routes": ["/stuck"]
  },

  "evidence": [                       // per-step structured evidence
    {
      "step": 4,
      "action": "click",
      "args": {"ref": "e012"},        // sensitive values redacted
      "ok": true,
      "failure": null,                // or {"kind": "timeout", "detail": "...", "retry_recommended": true}
      "url_before": "http://x/search",
      "url_after": "http://x/products",
      "route_changed": true,
      "attributed_requests": [        // network traffic caused by THIS action
        {"method": "GET", "url": "http://x/rest/items?q=apple", "status": 200, "resource_type": "xhr"}
      ],
      "screenshot": "/path/step_04.jpg",
      "data_keys": ["count", "items"],
      "duration_ms": 412.5
    }
  ],

  "app_graph": {                      // UI ↔ network ↔ route correlation
    "nodes": {"routes": ["/search"], "endpoints": ["GET /rest/items"]},
    "edges":  [{"step": 4, "action": "click", "ref": "e012",
                "url_before": "...", "url_after": "...",
                "route_changed": true, "endpoints": ["GET /rest/items"]}],
    "workflows": [{"step": 4, "action": "click", "workflow": "click /search → /products",
                   "route": "/products", "endpoints": ["GET /rest/items"]}],
    "action_endpoint_map": [{"action": "click", "ref": "e012", "endpoints": ["GET /rest/items"]}]
  },

  "auth_states": {                    // evidence-based verdict
    "state": "ANONYMOUS",             // ANONYMOUS|LOGIN_ATTEMPT|AUTHENTICATED|LOGIN_FAILED|MFA_REQUIRED|SESSION_EXPIRED
    "authenticated": false,           // only the machine sets this true
    "states_reached": ["ANONYMOUS"],
    "transitions": []                 // full evidence trail per transition
  },

  "blocked_targets": ["e017", "/stuck"],   // loop-escaped / failure-blocked
  "registry_stats": {"version": 14, "live_elements": 21, "tombstoned": 9,
                     "invalidated_total": 2, "total_interactions": 15},
  "browser_backend": "local-playwright"    // or "kernel"
}
```

### Failure vocabulary (stable contract)

`evidence[*].failure.kind` ∈ `unknown_ref | stale_ref | not_found |
timeout | not_actionable | detached | nav_rejected | provider_error |
browser_disconnected | exception`. SERA specialists can branch on these
deterministically.

## 5. The machine-readable run package

Every completed task writes `<session_root>/<task_id>/run_package.json`:

```jsonc
{
  "schema": "sera-browser.run-package/v1",
  "task_id": "...", "target": "...", "status": "completed",
  "application": {"title": "...", "technologies": ["Angular"],
                  "routes": ["/", "/login", "..."],
                  "forms": [{"action": "...", "method": "POST",
                             "has_password": true, "has_csrf": false, "field_count": 2}]},
  "workflows":  [ {"step": 4, "action": "click", "workflow": "click /search → /products",
                   "route": "/products", "endpoints": ["GET /rest/items"]} ],
  "endpoints":  ["GET /rest/items", "POST /api/feedback", "..."],
  "auth_states": {"state": "ANONYMOUS", "authenticated": false, "...": "..."},
  "network":    {"request_count": 312, "api_endpoints": [...], "graphql_operations": [...],
                 "websocket_endpoints": [...], "auth_indicators": [...],
                 "redirects": [["from", "to", 302]], "status_breakdown": {"200": 280}},
  "storage":    {"cookie_count": 3, "cookie_names": ["connect.sid", "..."],
                 "local_storage_keys": ["token"], "session_storage_keys": []},
  "evidence":   [ /* same shape as report.evidence, full ledger */ ],
  "coverage":   {/* same as report.coverage */},
  "blocked_targets": [...],
  "duration_ms": 84213.0, "steps": 25
}
```

SERA never needs Playwright knowledge: the package is pure application
intelligence. Recommended intake order: `application` → `endpoints` →
`workflows` → `auth_states` → `evidence` → `coverage`.

## 6. BrowserRuntime backends

```python
from sera_browser import (
    BrowserRuntime,            # protocol (action + observation surface)
    LocalPlaywrightRuntime,    # fully supported local backend
    KernelRuntime,             # remote kernel browser service
    create_runtime,            # async factory with auto health probe
    select_backend,            # sync settings resolution
)

rt, backend_name = await create_runtime(settings, task_id="t1",
                                        driver=driver, page=page, registry=registry)
```

- `SBA_BROWSER_BACKEND=local` (default) → LocalPlaywrightRuntime.
- `SBA_BROWSER_BACKEND=kernel` + `SBA_KERNEL_URL` → KernelRuntime
  (wire contract documented in
  `src/sera_browser/runtime/kernel.py`; requires the kernel browser
  service `/sessions` REST API).
- `SBA_BROWSER_BACKEND=auto` → kernel when a healthy `SBA_KERNEL_URL` is
  configured, else local. ONE bounded health probe; no retry loops.

## 7. LLM planner (optional, provider-agnostic)

Unchanged selection (`SBA_LLM_PROVIDER=none|zai|openai`), plus v4
guarantees:

- The LLM may only choose refs from the current registry view; invented
  refs are rejected before execution.
- Vision is budgeted (`SBA_VISION_MAX_CALLS`, default 2) with a
  consecutive-failure circuit breaker (`SBA_VISION_FAILURE_LIMIT`, default
  2). Provider errors never become a strategy.
- The planner receives structured step feedback (action result, failure
  kind, navigation change, new routes/refs, attributed traffic).

## 8. Environment variables (v4 additions)

| Variable | Default | Meaning |
|---|---|---|
| `SBA_ACTION_TIMEOUT_MS` | `8000` | ONE bounded action budget |
| `SBA_ACTION_RETRY_TIMEOUT_MS` | `3000` | the single retry's tight budget |
| `SBA_TOOL_REPEAT_LIMIT` | `2` | loop detection sensitivity |
| `SBA_FAILURE_BLOCK_LIMIT` | `2` | failures of same (action,ref) before blocking |
| `SBA_VISION_MAX_CALLS` | `2` | vision budget per task |
| `SBA_VISION_FAILURE_LIMIT` | `2` | vision circuit breaker threshold |
| `SBA_BROWSER_BACKEND` | `auto` | `auto \| local \| kernel` |
| `SBA_KERNEL_URL` | _(empty)_ | kernel browser service base URL |
| `SBA_KERNEL_TIMEOUT_S` | `10` | kernel request budget |

## 9. What SERA must NOT rely on (removed pathologies)

- ❌ `AgentReport.workflows` being heuristic labels (`"spa-routing"`,
  `"rest-api"`). v4 workflows are observed correlations from the app
  graph; the label strings only appear when no graph is available.
- ❌ `authentication.detected` implying an authenticated session. Auth is
  decided by the evidence-based state machine
  (`report.auth_states.authenticated`).
- ❌ Old ref formats (`button#N:…`, `linkN`, `formN_text_email`,
  `button_submit_N`). The single namespace is `e\d{3,}`.
- ❌ Long-click tolerance. Any single action is bounded to
  `action_timeout + retry_timeout` (~11s default worst case).
