# SERA Browser Agent — Architecture

## 1. Purpose

`sera-browser-agent` is a standalone, production-quality autonomous browser agent whose
**core engine is Playwright**. It receives a high-level objective (for example
"Analyze this web application and identify its structure, technologies,
authentication flows, routes, APIs, forms, business workflows and
security-relevant behavior") and autonomously drives a real browser, reasons
about structured browser state, escalates to vision only when needed, and
returns a deterministic structured report.

It is designed to be a reusable worker that SERA can later call without
needing to know any internal Playwright details.

## 2. Design Principles

1. **Playwright first.** The normal execution path is direct Playwright via the
   accessibility tree / DOM snapshot. We do not depend on Playwright MCP at
   runtime, and we do not depend on `browser-use` as the primary engine.
2. **Reason from state, not from rediscovery.** A single structured
   `BrowserState` object is maintained across the task; the agent reads it
   instead of re-scraping the page on every step.
3. **Deterministic element references.** Each interactive element receives a
   stable `ref` derived from its role + name + position in the accessibility
   tree (Playwright MCP-inspired). Actions target refs, not hand-rolled
   selectors. LLM-suggested selectors are never trusted blindly — they are
   validated against the current snapshot before use.
4. **Screenshots are an escalation path.** Vision is used only when visual
   layout matters, when information is only visually apparent, when an element
   cannot be identified structurally, or for visual verification / challenge
   pages.
5. **Bounded everything.** Every operation has a timeout, cancellation hook,
   timing record, and bounded retry. No infinite waits, no arbitrary sleeps.
6. **Security boundaries first.** SSRF protection, per-session browser
   isolation, no credential logging, allowlist-driven target validation,
   explicit local/lab mode for authorized targets.
7. **One browser lifecycle per task.** Cancellation propagates through the
   whole stack; force-cleanup guarantees no leaked Chromium / context / page /
   WebSocket / background task survives a cancelled task.

## 3. Component Map

```
BrowserAgent (orchestrator)
├── SessionManager        ── owns tasks, ids, lifecycle, isolation
├── BrowserManager        ── launches Chromium, manages contexts, modes
│   ├── ContextManager    ── isolated context per task, storage state
│   ├── PlaywrightDriver  ── direct Playwright primitives
│   ├── Inspector         ── DOM + accessibility snapshot, ref map
│   ├── RefMap            ── versioned ref map (stale-ref detection)
│   ├── ElementResolver   ── multi-strategy: ref→CSS→role→text→coord
│   ├── TabManager        ── multi-tab with stable short ids (t1, t2, ...)
│   ├── SnapshotTrimmer   ── intelligent trimming (cap ~50k chars)
│   ├── StealthLayer      ── pluggable anti-bot fingerprint evasion
│   └── SetOfMarkAnnotator── SoM vision fallback (bbox + numeric IDs)
├── BrowserState          ── structured, mutable, reason-from-this
├── NetworkRecorder       ── request/response/WS capture, redaction
├── StorageManager        ── cookies, localStorage, sessionStorage
├── ScreenshotLayer       ── capture-on-demand vision escalation
├── ChallengeDetector     ── CAPTCHA / Turnstile / Cloudflare / MFA
├── TaskPlanner           ── deterministic planner (default + fallback)
├── LLMTaskPlanner        ── LLM-backed planner (optional, validated)
│   ├── LLMClient (ABC)   ── provider-agnostic interface
│   │   ├── ZaiLLMClient       ── z-ai CLI subprocess
│   │   ├── OpenAICompatLLMClient ── OpenAI/Azure/OpenRouter/Ollama/...
│   │   └── MockLLMClient      ── canned responses for tests
│   ├── make_llm_client() ── factory: selects impl by provider
│   ├── action_schema     ── strict JSON action validator (refs, SSRF)
│   ├── MemoryManager     ── long_term_memory + rolling history + compaction
│   ├── SafetyGuard       ── dangerous-keyword blocking, action allowlist
│   └── ExtractionHelper  ── extract(prompt, schema) → validated JSON
├── ActionExecutor        ── executes refs/clicks/fills/bulk/tabs/drag/etc.
├── LoopRecoveryManager   ── tool + semantic repetition detection
├── CancellationManager   ── cooperative cancellation token
├── Observability         ── Playwright traces + structured action log + OTel
├── ReportBuilder         ── deterministic structured report
└── BrowserUseFallback    ── optional, shared-state fallback driver
```

### Core flow

```
Task
→ create isolated browser session       (SessionManager + BrowserManager)
→ navigate                              (PlaywrightDriver.navigate)
→ observe                               (Inspector.snapshot → BrowserState)
→ update state                          (BrowserState.update_from)
→ reason                                (TaskPlanner.next_step
│                                        OR LLMTaskPlanner.next_step)
│   ┌─ LLM planner: build structured state view → call LLM →
│   │  parse JSON → validate_action() → return Step
│   └─ fallback: deterministic planner takes over on any failure
→ act                                   (ActionExecutor.execute)
→ observe again
→ recover from failures/loops           (LoopRecoveryManager)
→ finish                                (ReportBuilder.build)
→ return structured report
→ cleanup                               (CancellationManager + force-cleanup)
```

### LLM planner flow (when `SBA_LLM_PROVIDER != none`)

```
observe state
→ LLMTaskPlanner builds compact BrowserState JSON view (≤12KB, no creds)
→ LLMClient.complete(system_prompt, state_view, timeout, cancellation)
→ parse_llm_response(raw)  ── tolerates markdown fences + leading prose
→ validate_action(parsed, state, settings)
│   ├─ action in allowed set?         (navigate/click/fill/.../finish)
│   ├─ args match per-action schema?
│   ├─ ref exists in interactive_elements?  (for click/fill/select/etc.)
│   └─ navigate passes validate_target()?   (SSRF + allowlist)
→ if valid: return Step to ActionExecutor
→ if invalid/unavailable/timeout/cancelled: deterministic fallback
```

### LLM layer (provider-agnostic)

The Browser Agent does NOT hardcode any specific LLM provider. The LLM layer
is a clean abstract interface with a factory:

```
LLMConfig(provider, model, api_key, base_url, timeout_s, extra)
    │
    ▼
make_llm_client(config)  ── factory selects implementation
    │
    ├── provider="none"   → _NoneLLMClient (always unavailable → deterministic)
    ├── provider="zai"    → ZaiLLMClient (z-ai CLI subprocess)
    ├── provider="openai" → OpenAICompatLLMClient (OpenAI/Azure/OpenRouter/
    │                                       Together/Anyscale/vLLM/Ollama/...)
    └── (future providers plug in here)
```

`LLMClient` (ABC) interface:
- `async complete(system, user, timeout_s, cancellation) -> LLMResponse`
- `async complete_with_vision(system, user, image_png, timeout_s, cancellation) -> LLMResponse` (optional)
- `available() -> bool`
- `provider_name -> str`
- `supports_vision -> bool`

Error classification (all subclasses of `LLMError`):
- `LLMUnavailableError` — provider not installed / not configured / 5xx
- `LLMTimeoutError` — call exceeded `timeout_s`
- `LLMAuthError` — 401 / 403 (bad API key)
- `LLMRateLimitError` — 429 (quota exceeded)
- `LLMResponseError` — malformed / empty response

`LLMResponse`:
- `content: str` — raw text from the model
- `parsed_json` — cached property, parses content as JSON (tolerates fences/prose)
- `provider`, `model`, `usage`, `finish_reason` — metadata

**Vision is optional and never sent on every step.** The LLM planner only
escalates to vision when:
1. loop recovery triggers (semantic repetition detected), AND
2. `client.supports_vision` is True, AND
3. a screenshot provider is configured.

`LLMPlannerStats` records `vision_calls` and `vision_succeeded` for visibility.

## 4. Browser State

`BrowserState` is the single source of truth the agent reasons from. It carries
at least the fields listed in the project contract:

`current_url`, `title`, `page_type`, `navigation_history`, `authenticated`,
`user_role`, `forms`, `links`, `interactive_elements`, `routes`,
`api_endpoints`, `requests`, `responses`, `cookies`, `local_storage_keys`,
`session_storage_keys`, `console_errors`, `page_errors`, `websockets`,
`downloads`, `screenshots`, `dom/accessibility snapshot`, `dom_hash`,
`last_action`, `last_observation`, `timings`.

Snapshots are hashed (`dom_hash`) so loop detection can detect when the page
has not changed across observations.

## 5. Playwright Primitives

Implemented directly in `PlaywrightDriver` (no MCP dependency):

`navigate`, `back`, `forward`, `reload`, `snapshot` (accessibility tree),
DOM inspection, `find_elements`, `click`, `fill`/`type`, `select`,
checkbox/radio, `keyboard`, `mouse`, `hover`, `scroll`, `screenshot`,
tabs/popups, cookies, `localStorage`/`sessionStorage`, current URL/title,
console messages, page errors, downloads, dialogs, network requests/responses,
WebSocket activity, JavaScript evaluation (gated behind an explicit allow
flag).

`navigate(url)` is idempotent: if the requested URL equals the current URL
**and** no reload was requested, the driver inspects the current state instead
of navigating again. This avoids unnecessary reloads and burning time on SPA
pages.

## 6. Vision Escalation

`ScreenshotLayer.capture()` is called only when:

- visual layout matters,
- important information is only visually apparent,
- an element cannot be reliably identified structurally,
- a visual verification step is needed, or
- a challenge page requires visual understanding.

Screenshots are stored on disk under the task's session dir and referenced by
path in the final report; they are not embedded in every step's payload sent
to the model.

## 7. Optional `browser-use` Fallback

`BrowserUseFallback` is an **optional** driver. It is imported lazily and only
invoked when:

1. the Playwright path has exhausted its retries / cannot complete the task,
2. the configured fallback policy permits it, and
3. the `browser-use` package is importable.

It must reuse the same `BrowserState`, `SessionManager`, `CancellationManager`,
`NetworkRecorder`, and result format. It does **not** spawn its own browser
lifecycle — it borrows the page/context managed by `BrowserManager` when
possible, or launches a context that the same manager tracks. The normal path
never touches `browser-use`.

## 8. Challenges / CAPTCHA / Bot Protection

`ChallengeDetector` classifies:

- CAPTCHA (generic)
- Cloudflare Turnstile
- Cloudflare "checking your browser" interstitial
- hCaptcha
- reCAPTCHA v2/v3
- "verify you are human" interstitials
- bot detection walls
- login challenges
- MFA / 2FA prompts

For **authorized** testing the agent:

1. detects the challenge,
2. classifies it,
3. records it in `BrowserState.challenges`,
4. checks whether the environment provides an official test mechanism
   (e.g. Cloudflare Turnstile test keys — documented in
   `docs/BROWSER-AUTOMATION.md`),
5. uses the official test configuration where available,
6. otherwise pauses and requests human-in-the-loop interaction,
7. resumes automatically after the challenge is legitimately completed.

The agent **does not** implement techniques intended to bypass or defeat
third-party CAPTCHA or anti-bot protections.

## 9. Timeouts & Performance

- Every operation has an explicit timeout configured via `Settings`.
- Navigation distinguishes DNS/connect, page navigation, DOMContentLoaded,
  load, SPA readiness, and optional network-idle waiting.
- `network-idle` is bounded — never waits indefinitely.
- Navigation duration is measured and logged into `BrowserState.timings`.
- Long-running operations periodically check the cancellation token.

## 10. Loop Detection

Two layers:

1. **Tool repetition** — the same tool+args called N times in a row with no
   state change.
2. **Semantic / browser-state repetition** — `dom_hash` did not change after
   the last action, or the same `(navigate, observe)` cycle repeats without
   progress.

On detection the agent stops repeating, re-observes the current state, tries
an alternate observation method, changes strategy, or declares the task
blocked. The LLM never burns its entire tool budget repeating a failed
action.

## 11. Network Intelligence

`NetworkRecorder` captures structured records: method, URL, status, request
headers, response headers, request size, response size, resource type,
initiator, timing, cookies, auth indicators, GraphQL requests, WebSocket
connections, redirects. Sensitive headers (`Authorization`, `Cookie`,
`Set-Cookie`, custom auth headers) are redacted unless explicitly enabled by a
flag reserved for trusted lab targets.

The recorder is reusable as a security sensor for SERA.

## 12. Authentication

Supported attachment modes:

- username/password forms (filled via Playwright primitives),
- session cookies,
- bearer tokens (injection via `extraHTTPHeaders`),
- custom headers,
- API tokens,
- pre-authenticated Playwright `storage_state` files,
- login persistence (save `storage_state` after a successful login),
- separate browser identities (one isolated context per assessment).

Credentials are **never** written to logs. Saved `storage_state` is treated
as sensitive (Playwright recommendation) and stored under the task's session
directory with restrictive permissions.

## 13. Browser Modes

`auto` (default = headless in CI, headed if a display is available),
`headed`, `headless`. The setting is applied at launch/context creation and
exposed in `status` and the final report.

## 14. Browser Isolation & Cancellation

Each task has a clear owner: `create → use → cancel → close → cleanup`.

Cancellation is cooperative and immediate. Every long operation checks the
cancellation token periodically. `cancel_task(task_id)` propagates through:

```
Task → Planner → ActionExecutor → PlaywrightDriver → (fallback) →
       ScreenshotLayer → NetworkRecorder
```

`force_cleanup` is the final safety net: it kills the Chromium process tree,
closes contexts/pages, aborts in-flight requests, and tears down background
tasks before the task is marked finished.

## 15. API

Small HTTP API (FastAPI):

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| POST   | `/tasks`                   | Create + start a browser agent task      |
| GET    | `/tasks/{id}`              | Task status + metadata                   |
| POST   | `/tasks/{id}/cancel`       | Cancel a running task                    |
| GET    | `/tasks/{id}/state`        | Current `BrowserState` snapshot          |
| GET    | `/tasks/{id}/result`       | Final structured report                  |
| GET    | `/health`                  | Liveness + effective browser mode        |

`POST /tasks` body accepts: `target`, `task`, `browser_mode`,
`auth`/`session` options, `timeout`, `allowed_domains`/`allowed_targets`,
`local_lab` flag.

## 16. Security Boundaries

- No execution of arbitrary host shell commands.
- SSRF protection is **on by default**; only `local_lab` mode allows private
  network targets, and only when the target resolves to a configured allowlist.
- Per-task browser contexts never share storage state unless explicitly
  attached via `storage_state`.
- One task cannot control another task's browser.
- LLM-suggested selectors are validated against the current snapshot.
- Credentials are never logged.

## 17. Integration Contract with SERA

SERA will eventually send a request like:

> "Analyze this application for business flows, authentication, APIs, routes,
> technologies and security-relevant browser behavior."

The Browser Agent returns a deterministic structured report:

```json
{
  "status": "completed",
  "target": "...",
  "title": "...",
  "technology": [],
  "routes": [],
  "apis": [],
  "forms": [],
  "authentication": {},
  "workflows": [],
  "network": {},
  "screenshots": [],
  "console_errors": [],
  "challenges": [],
  "observations": [],
  "summary": "..."
}
```

SERA does not need to know any internal Playwright details. The integration
itself is **not** implemented in this project — only the contract.

## 18. Project Layout

```
sera-browser-agent/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md          ← this file
│   ├── BROWSER-AUTOMATION.md    ← operational manual
│   └── DECISIONS.md             ← ADR log
├── src/sera_browser_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py                ← Settings (env-driven)
│   ├── logging.py               ← structured logging
│   ├── models.py                ← Pydantic types / BrowserState
│   ├── security.py              ← SSRF / allowlist / target validation
│   ├── cancel.py                ← CancellationManager
│   ├── browser/                 ← PlaywrightDriver, Manager, Context, ...
│   ├── state/                   ← BrowserState
│   ├── net/                     ← NetworkRecorder
│   ├── challenge/               ← ChallengeDetector
│   ├── task/                    ← Planner, Executor, Loop, Agent, Report
│   │   ├── planner.py           ← deterministic TaskPlanner (default + fallback)
│   │   ├── llm_planner.py       ← LLMTaskPlanner (structured state → LLM → validate)
│   │   ├── llm_client.py        ← LLMClient ABC + LLMConfig + make_llm_client factory
│   │   │                         + ZaiLLMClient + OpenAICompatLLMClient + MockLLMClient
│   │   ├── action_schema.py     ← strict JSON action validator (refs, SSRF)
│   │   ├── executor.py          ← ActionExecutor (ref-based Playwright execution)
│   │   ├── loop.py              ← LoopRecoveryManager
│   │   ├── agent.py             ← BrowserAgent orchestrator
│   │   └── report.py            ← ReportBuilder
│   ├── session/                 ← SessionManager
│   └── api/                     ← FastAPI app
├── tests/
│   ├── fixtures/local_site/     ← local FastAPI test site
│   └── test_*.py
└── scripts/
    └── run_juice_shop_demo.py
```

## 19. End State (target)

```
SERA
  ↓
Browser Agent task
  ↓
Playwright BrowserService
  ↓
Browser state + traffic + observations
  ↓
Autonomous Browser Agent reasoning
  ↓
Structured browser report
  ↓
SERA orchestrator
```
