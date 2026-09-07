# SERA Browser Agent — Decision Log (ADR)

## ADR-0001 — Playwright is the primary browser engine

**Status:** Accepted
**Date:** 2026-08-30

### Context

The project requires a production-quality autonomous browser agent. Two
options were considered as the primary engine: Microsoft's Playwright MCP
(model-context-protocol server) and direct async Playwright.

### Decision

Use **direct async Playwright** as the primary engine. Adopt the
**architectural ideas** of Playwright MCP (accessibility-tree/snapshot-driven
interaction, deterministic element references, structured browser state,
screenshots-only-when-useful, persistent/isolated contexts, clean session
lifecycle) but do not depend on Playwright MCP at runtime.

### Consequences

- No MCP server runtime dependency.
- We own the snapshot/ref-map format and can evolve it independently.
- Direct Playwright gives us finer control over timeouts, cancellation,
  network recording, and cleanup than going through an MCP wrapper.

---

## ADR-0002 — `browser-use` is an optional fallback, never the normal path

**Status:** Accepted
**Date:** 2026-08-30

### Context

`browser-use` provides an LLM-driven browser loop. It is convenient for
genuinely difficult visual interactions but is heavy, slower, and
non-deterministic compared with direct Playwright.

### Decision

- `browser-use` is **optional**. It is imported lazily and only when
  `SBA_ENABLE_BROWSER_USE_FALLBACK=true` **and** the package is importable.
- The fallback shares `BrowserState`, `SessionManager`, `CancellationManager`,
  `NetworkRecorder`, and the same result format — it never spawns a parallel
  browser lifecycle.
- The normal execution path never touches `browser-use`.

### Consequences

- Default install has zero `browser-use` dependency.
- Vision-heavy interactions can still be delegated when needed.
- Architecture stays clean: one engine, one lifecycle, with a pluggable
  driver interface.

---

## ADR-0003 — Single structured `BrowserState`, reason-from-state

**Status:** Accepted
**Date:** 2026-08-30

### Context

LLM agents frequently re-scrape the page on every step, wasting tokens and
time. They also forget what they tried, leading to loops.

### Decision

Maintain a single `BrowserState` object per task. The agent reads from it
instead of re-discovering the page. Snapshots are hashed (`dom_hash`) so loop
detection can detect "the page didn't change after the last action".

### Consequences

- Cheaper reasoning, fewer browser round-trips.
- Loop detection has a structural signal to key on.
- State must be cheap to read and append-only in places (network, errors).

---

## ADR-0004 — Deterministic element refs, never trust LLM selectors blindly

**Status:** Accepted
**Date:** 2026-08-30

### Context

LLM-suggested CSS selectors are often wrong, stale, or unsafe (e.g.
`div:nth-child(3) > a`).

### Decision

Each interactive element gets a stable `ref` derived from
`(role, name, index_in_role)` from the accessibility snapshot. Actions target
refs. LLM-suggested selectors are accepted only if they resolve to an element
that is **also** present in the current snapshot.

### Consequences

- More reliable clicks/fills.
- Fewer flaky failures.
- Slight implementation cost on the ref-map builder.

---

## ADR-0005 — Screenshots are an escalation path, not the default

**Status:** Accepted
**Date:** 2026-08-30

### Context

Sending screenshots to a vision model on every step is expensive and slow.

### Decision

Screenshots are captured only when: visual layout matters, information is
only visually apparent, an element cannot be reliably identified
structurally, a visual verification step is needed, or a challenge page
requires visual understanding.

### Consequences

- Normal path is fast and cheap.
- Vision budget is reserved for cases that actually need it.

---

## ADR-0006 — Idempotent `navigate`

**Status:** Accepted
**Date:** 2026-08-30

### Context

SPAs and many agent loops call `navigate(current_url)` repeatedly, causing
unnecessary reloads.

### Decision

`navigate(url)` checks `url == current_url`. If equal and `reload` was not
explicitly requested, it only refreshes the snapshot/state. Otherwise it
navigates.

### Consequences

- Faster loops on SPAs.
- No accidental state loss from spurious reloads.

---

## ADR-0007 — Bounded everything; cooperative cancellation + force-cleanup

**Status:** Accepted
**Date:** 2026-08-30

### Context

Autonomous browser tasks can hang forever on network-idle, modal dialogs,
or infinite loops.

### Decision

- Every operation has a timeout.
- Every long operation checks a cancellation token periodically.
- `cancel_task(task_id)` propagates through the whole stack.
- `force_cleanup` is a final safety net that kills the Chromium process tree
  even if cooperative cancellation fails.

### Consequences

- No leaked Chromium processes after a cancelled task.
- Predictable resource usage.
- Test `test_cancellation` and `test_browser_crash_recovery` enforce this.

---

## ADR-0008 — NetworkRecorder is a reusable security sensor

**Status:** Accepted
**Date:** 2026-08-30

### Context

SERA needs structured traffic intelligence: APIs, GraphQL, WebSockets,
auth indicators, redirects.

### Decision

`NetworkRecorder` captures structured per-request/response records. Sensitive
headers are redacted by default. The recorder is designed to be reused as a
standalone security sensor for SERA, independent of the agent loop.

### Consequences

- SERA can later consume the recorder directly.
- Auth indicators (`Authorization`, `Set-Cookie`, custom) feed the agent's
  `authentication` inference without storing raw secrets.

---

## ADR-0009 — SSRF protection on by default; explicit local-lab allowlist

**Status:** Accepted
**Date:** 2026-08-30

### Context

A browser agent that can hit arbitrary URLs is an SSRF vehicle.

### Decision

- SSRF protection is on by default.
- Private/loopback/link-local addresses are rejected unless `local_lab=true`
  **and** the target is in `allowed_targets` /
  `SBA_ALLOWED_PRIVATE_TARGETS`.
- Cloud-metadata endpoints (`169.254.169.254`) are always rejected.

### Consequences

- The agent cannot be used as an SSRF proxy by default.
- Authorized internal/lab testing is still possible but explicit.

---

## ADR-0010 — One HTTP API surface; SERA integration not implemented here

**Status:** Accepted
**Date:** 2026-08-30

### Context

SERA will eventually call this agent as a worker, but we must not implement
the integration yet.

### Decision

Implement a small FastAPI surface (`/tasks`, `/tasks/{id}`,
`/tasks/{id}/cancel`, `/tasks/{id}/state`, `/tasks/{id}/result`, `/health`).
Design the request/response contract so SERA can call it without Playwright
knowledge. Do **not** add SERA-specific imports, adapters, or coupling.

### Consequences

- Clean reusable worker.
- SERA integration is a future, separate change set.

---

## ADR-0011 — No existing SERA code in this environment

**Status:** Accepted
**Date:** 2026-08-30

### Context

The user requested we inspect the existing SERA browser implementation
before coding. A filesystem search found **no SERA source tree** in this
environment.

### Decision

- Proceed by designing the integration contract from the user's specification.
- Do not invent a fake SERA module.
- When SERA is later present, the contract in `docs/ARCHITECTURE.md`
  §17 defines exactly what SERA should send and what the agent returns.

### Consequences

- We do not block on missing SERA code.
- The contract is explicit and reviewable.

---

## ADR-0012 — OWASP Juice Shop via npm, not Docker

**Status:** Accepted
**Date:** 2026-08-30

### Context

Docker is not available in this environment. Node 24 is.

### Decision

Run OWASP Juice Shop via `npm install -g juice-shop && juice-shop` (or
`npx juice-shop`). The test suite marks Juice Shop tests
`@pytest.mark.juice_shop` and skips them when Juice Shop is unreachable, so
the rest of the suite always runs against the bundled local test site.

### Consequences

- Demo works without Docker.
- Local test site (bundled FastAPI app) is the always-available fallback.

---

## ADR-0013 — Logging never contains credentials

**Status:** Accepted
**Date:** 2026-08-30

### Context

Browser automation inherently handles credentials.

### Decision

- Structured JSON logging via `structlog`-style helper (stdlib `logging`
  + JSON formatter).
- A `redact()` helper masks `Authorization`, `Cookie`, `Set-Cookie`,
  `password`, `token`, `secret`, `api_key` keys/headers before any log line.
- Saved `storage_state` is treated as sensitive (Playwright recommendation),
  stored under the task's session dir with `0o600` permissions.

### Consequences

- Logs are safe to ship to a central system.
- Storage state files never get committed accidentally (`.gitignore`).

---

## ADR-0014 — LLM planner with deterministic fallback; LLM never executes Playwright directly

**Status:** Accepted
**Date:** 2026-08-30

### Context

The deterministic planner is reliable but cannot genuinely "reason" about a
high-level objective. An LLM-backed planner can, but giving an LLM direct
access to Playwright primitives is unsafe (arbitrary code execution,
unbounded actions, SSRF via `page.goto`).

### Decision

Add an `LLMTaskPlanner` that:

1. Receives a **structured `BrowserState` view** (JSON), not raw Playwright
   internals. The view is compact (≤12KB), curated, and contains no
   credentials.
2. Returns a **single JSON object** `{thinking, action, args, reason}` from
   a fixed action set (`navigate`, `click`, `fill`, `select`, `check`,
   `uncheck`, `hover`, `scroll`, `press`, `screenshot`, `finish`, `blocked`).
3. The response is **validated** by `action_schema.validate_action()`:
   - action name must be in the allowed set
   - args must match the per-action schema
   - ref-based actions must reference an element present in the current
     `state.interactive_elements`
   - `navigate` must pass `validate_target()` (SSRF + allowlist)
4. The validated `Step` is handed to the existing `ActionExecutor` — the
   LLM never touches Playwright directly.

**Fallback to the deterministic planner** happens when:
- `SBA_LLM_PROVIDER=none` (default; zero LLM dependency)
- the LLM client is unavailable (z-ai CLI not on PATH)
- the LLM call times out (`SBA_LLM_TIMEOUT_S`)
- the LLM returns unparseable JSON
- the LLM returns an action that fails validation
- the LLM call is cancelled (re-raises `TaskCancelledError` so the
  orchestrator cleans up)

Loop detection, cancellation, timeouts, and same-URL idempotency are all
preserved — the LLM planner delegates to `LoopRecoveryManager` and
`CancellationManager` exactly like the deterministic planner.

The LLM provider is pluggable via the `LLMClient` protocol.
`ZaiLLMClient` calls the `z-ai` CLI as a subprocess; `MockLLMClient` is
used in tests.

### Consequences

- LLM cannot execute arbitrary code.
- LLM cannot navigate to SSRF targets (validator enforces allowlist).
- LLM cannot click/fill non-existent refs (validator checks snapshot).
- Agent degrades gracefully to deterministic when LLM is unavailable.
- Default install has zero LLM dependency (`SBA_LLM_PROVIDER=none`).
- LLM planner stats (`calls`, `succeeded`, `fallbacks`, `invalid`) are
  recorded as an observation in the final report for SERA visibility.

---

## ADR-0015 — Provider-agnostic LLM layer; Z.ai is one implementation

**Status:** Accepted
**Date:** 2026-08-30

### Context

ADR-0014 introduced the LLM planner with `ZaiLLMClient` as the concrete
implementation. Before integrating into SERA, the LLM layer must be
provider-agnostic so SERA (or any other consumer) can plug in any LLM
provider without changing the Browser Agent architecture.

### Decision

Refactor `llm_client.py` into a clean provider-agnostic layer:

1. **`LLMClient` ABC** — the interface every implementation must satisfy:
   - `async complete(system, user, timeout_s, cancellation) -> LLMResponse`
   - `async complete_with_vision(system, user, image_png, timeout_s, cancellation) -> LLMResponse` (optional)
   - `available() -> bool`
   - `provider_name -> str`
   - `supports_vision -> bool`

2. **`LLMConfig`** — provider-agnostic configuration dataclass:
   `provider`, `model`, `api_key`, `base_url`, `timeout_s`, `extra`.
   Includes `masked_repr()` for safe logging.

3. **`make_llm_client(config)` factory** — selects the implementation by
   `config.provider`:
   - `"none"` → `_NoneLLMClient` (always unavailable → deterministic fallback)
   - `"zai"` → `ZaiLLMClient` (z-ai CLI subprocess)
   - `"openai"` → `OpenAICompatLLMClient` (OpenAI Chat Completions REST API)
   - Future providers plug in here without touching the Browser Agent.

4. **Concrete implementations:**
   - `ZaiLLMClient` — calls the `z-ai` CLI as a subprocess.
   - `OpenAICompatLLMClient` — speaks the OpenAI Chat Completions REST API
     via `httpx.AsyncClient`. Works with OpenAI, Azure OpenAI, OpenRouter,
     Together, Anyscale, vLLM, Ollama, LM Studio, and any
     OpenAI-compatible endpoint. Does NOT require the `openai` Python
     package — we speak the REST API directly so we have full control over
     timeouts, cancellation, and error classification.
   - `MockLLMClient` — canned responses for tests; supports vision toggle.

5. **Error classification** — all errors subclass `LLMError`:
   - `LLMUnavailableError` — provider not installed / not configured / 5xx
   - `LLMTimeoutError` — call exceeded `timeout_s`
   - `LLMAuthError` — 401 / 403 (bad API key)
   - `LLMRateLimitError` — 429 (quota exceeded)
   - `LLMResponseError` — malformed / empty response

6. **`LLMResponse`** — normalized response with `content` (str),
   `parsed_json` (cached, tolerant of markdown fences + leading prose),
   and metadata (`provider`, `model`, `usage`, `finish_reason`).

7. **`Settings.to_llm_config()`** — builds an `LLMConfig` from the
   `SBA_LLM_*` env vars so the agent doesn't reference `LLMConfig` fields
   directly.

8. **`BrowserAgent._build_planner()`** — uses the factory instead of
   hardcoding `ZaiLLMClient`. The agent receives `(provider, model,
   api_key, base_url)` via settings and calls `make_llm_client()` once
   per task.

9. **Vision is optional** — `LLMClient.supports_vision` defaults to
   `False`. The planner NEVER sends screenshots on every step. Vision
   escalation happens ONLY when loop recovery triggers AND the client
   supports vision AND a screenshot provider is configured.

### Consequences

- The Browser Agent architecture has zero Z.ai-specific coupling.
- SERA can plug in any OpenAI-compatible provider by setting 4 env vars.
- Adding a new provider (e.g. Anthropic native API, Bedrock, Vertex) only
  requires implementing `LLMClient` + adding one branch to the factory.
- Error classification lets the planner distinguish "auth failed" (don't
  retry) from "rate limited" (could retry with backoff) from "unavailable"
  (fall back to deterministic).
- Vision budget is preserved — screenshots are only sent on the
  loop-recovery escalation path.
- `LLMConfig.masked_repr()` ensures API keys never leak into logs.
- Tests use `MockLLMClient` + httpx `MockTransport` — no real network
  calls, deterministic assertions.

---

## ADR-0016 — Advanced production features (Tier 0 + Tier 1 + Tier 2)

**Status:** Accepted
**Date:** 2026-08-31

### Context

After the provider-agnostic LLM layer (ADR-0015), research into SOTA browser
agents (browser-use, Playwright MCP, Skyvern, Stagehand, WebVoyager, AgentE)
revealed 25 features that separate "demo" from "production-grade." This ADR
records the decision to implement Tier 0 + Tier 1 + key Tier 2 features.

### Decision

Implement the following advanced features, each inspired by specific SOTA
projects and documented in the research report:

**Tier 0 (foundational):**
1. **Versioned ref map** (`browser/refmap.py`) — monotonic snapshot versions
   + stable integer indices + content signatures. Stale-ref detection rejects
   actions whose version ≠ current snapshot. (survey §5.1.1, browser-use)
2. **Bulk/batched actions** — `fill_form` action fills multiple fields in one
   call (74% fewer tool calls per survey §5.3). Added to action_schema + executor.
3. **Failure-adaptation protocol** — fresh snapshot on failure + "never retry
   same action twice" system rule in the planner prompt. (survey §5.4)
4. **Memory + compaction** (`task/memory.py`) — `long_term_memory` field +
   rolling step history buffer (last 40 full, older summarized). (browser-use,
   survey §6.3)
5. **Cache-friendly context ordering** — state view ordered: stable → memory →
   tabs → history → snapshot (most-volatile last) for prefix-cache hits (~89%
   cost reduction per survey §7.2).

**Tier 1 (high-leverage differentiators):**
6. **Multi-strategy element resolver** (`browser/resolver.py`) — ref → CSS →
   role+name → text → coordinate, with self-healing. (Skyvern mode-3, Stagehand)
7. **Multi-tab manager** (`browser/tabs.py`) — stable short ids (t1, t2, ...),
   switch/close/new_tab actions, popup tracking. (browser-use, Playwright MCP)
8. **Intelligent snapshot trimming** (`browser/trimmer.py`) — caps at ~50k chars,
   keeps all interactive elements, hides repetitive list items. (survey §6.2)
9. **Observability** (`task/observability.py`) — Playwright traces (.zip) +
   structured action log (JSONL) + OTel-ready spans. (Playwright MCP, Stagehand)
10. **Deterministic safety boundaries** (`task/safety.py`) — dangerous-keyword
    blocking (delete/transfer/refund/purchase/etc.), action allowlist (readonly
    vs full), domain blocklist. Enforced in code, not the LLM prompt. (survey §8.4)
11. **Rich action set** — drag, upload_file, save_pdf, search_page, find_elements,
    extract, evaluate, switch_tab/close_tab/new_tab, mouse_click (coordinates).

**Tier 2 (scale/enterprise readiness):**
12. **Pluggable stealth layer** (`browser/stealth.py`) — UA/viewport/timezone
    randomization, navigator.webdriver removal, canvas/WebGL fingerprint noise,
    proxy support. (playwright-stealth, fingerprint-suite)
13. **Set-of-Mark vision fallback** (`browser/som.py`) — bounding-box + numeric-ID
    overlays on screenshots for VLM-discrete action vocabulary. (WebVoyager,
    VisualWebArena)
14. **Structured extraction** (`task/extraction.py`) — `extract(prompt, schema)`
    with lightweight JSON-schema validation (no `jsonschema` dependency).
    (browser-use, Skyvern, Stagehand)
15. **flash_mode** — shortened prompts (no reflection) for routine steps.
    (browser-use)

### Consequences

- The agent is now production-grade: versioned refs prevent stale-action
  errors, bulk actions cut form-filling cost by 74%, memory keeps token cost
  flat over long tasks, safety boundaries prevent prompt-injection hijacking.
- Vision is truly optional and never default — SoM is a fallback when the AX
  tree is empty, not a primary interaction mode.
- Stealth is opt-in (`SBA_STEALTH_LEVEL`) and does NOT bypass CAPTCHA.
- Observability (traces + action log) makes debugging long-horizon failures
  tractable.
- 174 tests cover all new features including real-browser integration tests.

---

## ADR-0017 — Recommended follow-ups: stealth, CDP AX tree, eval harness, PAV pattern

**Status:** Accepted
**Date:** 2026-08-31

### Context

After ADR-0016 (advanced features), four follow-ups were identified to
further productionize the agent: stealth integration, CDP AX tree, eval
harness, and the Plan-Act-Validate multi-agent pattern.

### Decision

1. **Stealth wired into ContextManager** — `SBA_STEALTH_LEVEL` (off|light|
   medium|aggressive) now applies fingerprint randomization (UA, viewport,
   timezone, locale, proxy) + init scripts (navigator.webdriver removal,
   canvas/WebGL noise) to every context opened. Configured via env vars
   `SBA_STEALTH_*`. Does NOT bypass CAPTCHA — only reduces automation
   fingerprint for legitimate testing.

2. **CDP-based AX tree extractor** (`browser/cdp_ax.py`) — uses Chrome
   DevTools Protocol `Accessibility.getFullAXTree` for richer ARIA semantics,
   shadow-DOM piercing, and backendNodeId-based element resolution. The
   driver tries CDP first (when `SBA_SNAPSHOT_METHOD=cdp|auto`), falls back
   to the DOM walker on failure. Default is `dom` (fast); `cdp` for richer
   semantics; `auto` for best-of-both with latency tradeoff.

3. **Eval harness** (`eval/harness.py`) — WebArena/Mind2Web-style task suite
   with 5 default tasks against the local test site. Supports two judges:
   - `DeterministicJudge`: checks URL/title/API/form/routes conditions (fast,
     free, CI-friendly)
   - `LLMJudge`: asks an LLM to grade the report (accurate, costs tokens)
   The harness is framework-agnostic (no pytest dependency) and produces a
   summary with pass_rate, avg_score, and per-task details.

4. **Plan-Act-Validate multi-agent pattern** (`task/pav.py`) — Skyvern-style
   Planner/Actor/Validator with Reflexion. Enabled via
   `SBA_AGENT_PATTERN=plan_act_validate`. The Planner decomposes the
   objective into sub-goals; the Actor (existing LLMTaskPlanner) executes
   each; the Validator checks achievement; on failure beyond max_attempts,
   the Planner re-plans (Reflexion). All advanced features (memory, safety,
   observability) are inherited via the Actor.

### Consequences

- Stealth is now a first-class configuration, not a separate layer to wire.
- CDP AX tree gives production-grade element identification (richer than DOM).
- The eval harness enables measuring improvements vs. regressions.
- PAV pattern improves long-horizon task success (Plan-and-Execute + Validator
  beats raw ReAct per the survey).
- 195 tests passing (174 existing + 21 new follow-up tests).

---

## ADR-0018 — dsh-browser-inspired features: scrape, content, history/replay, soft restrict, cookie auth, deep challenge scan

**Status:** Accepted
**Date:** 2026-08-31

### Context

After analyzing `wqty123/dsh-browser` (a TypeScript/Electron browser plugin for
DeepSeek Harness), we identified 6 practical interaction primitives that
dsh-browser has and we lacked. These are complementary to our agent
architecture (PAV, memory, stealth, CDP AX, eval harness) — they fill gaps
in day-to-day browser interaction.

### Decision

Port 6 features from dsh-browser, adapting them to our Python/Playwright
architecture:

1. **`scrape` action** (`task/executor.py::_scrape`) — CSS-based structured
   extraction with `selector@attr` syntax (e.g. `a@href` returns absolute
   URLs). Static CSS only — no arbitrary JS — so it's CSP-safe. Returns
   `{count, items[]}`. No LLM call needed (unlike `extract`).

2. **`content` action** (`task/executor.py::_fetch_content`) — multi-format
   page content fetch: `html` | `markdown` | `txt` | `json`. Optional CSS
   selector scoping + `max_chars` cap. Lightweight HTML→markdown converter.

3. **History + Replay** (`task/history.py`) — records every browser op
   (`seq`, `action`, `params`, `ok`, `result`, `error`, `at`) into a bounded
   (500-entry) log. `replay(seq)` re-issues replayable actions. Sensitive
   fields (typed text, values, execute scripts) are masked in model-facing
   output.

4. **`restrict` action** (`task/soft_restrict.py`) — soft model-controllable
   action allowlist. Read-only actions always allowed. Explicitly a SOFT
   guardrail (not a security boundary — the model can lift it itself).

5. **`flush_auth` / `restore_auth` actions** — cookie export/import for
   session persistence. `flush_auth` returns the cookie list; `restore_auth`
   re-imports them. Treated as live credentials (never logged).

6. **Deep challenge scan** (`challenge/detector.py::_deep_scan`) — runs a
   page-context JS expression that scans shadow roots + same-origin iframes
   for captcha markers (Cloudflare/hCaptcha/reCAPTCHA/Turnstile/generic with
   bilingual EN+中文 wording). Catches challenges hidden in shadow DOM.

### Consequences

- The agent now has a complete practical interaction vocabulary: scrape for
  structured extraction, content for page reading, history/replay for
  reproducibility, restrict for self-guarding, cookie auth for persistence,
  and deep challenge detection for shadow-DOM captcha.
- 225 tests passing (195 existing + 30 new dsh-browser feature tests).
- All features are opt-in via the action schema (the LLM can request them).
- dsh-browser's React/Vue-safe native setter is already covered by our
  Playwright `locator.fill()` which uses native setters internally.

---

## ADR-0019 — Real-view browser viewer (human-takeoverable, for SERA chat UI)

**Status:** Accepted
**Date:** 2026-08-31

### Context

The SERA assessment chat UI needs to show the browser in real-time — not a
static screenshot, but a live view where the human can watch every step the
agent takes and grab control at any time. This is the "real view, not a
relay" paradigm from dsh-browser, adapted to our Python/Playwright architecture.

### Decision

Implement a three-layer real-view system:

1. **CDP remote debugging** (`BrowserManager`) — when `SBA_ENABLE_VIEWER=true`,
   Chromium launches with `--remote-debugging-port` (default 9222). The SERA
   UI (or `chrome://inspect`) can connect to the same browser instance for
   full interactive access. The CDP URL is exposed in `/tasks/{id}/viewer`.

2. **Screenshot + state streaming** (`browser/viewer.py::BrowserViewer`) —
   after each agent step, a JPEG screenshot + current URL/title/step info is
   streamed to connected WebSocket clients via `WS /tasks/{id}/stream`. The
   SERA chat UI connects here to receive real-time screenshots.

3. **Human takeover** (`BrowserViewer.request_takeover` + `wait_for_resume`) —
   the agent or the human (via `POST /tasks/{id}/human_takeover`) triggers a
   takeover. The agent pauses between steps, the human interacts with the
   visible browser window, then calls `POST /tasks/{id}/resume`. The agent
   re-observes and continues.

New API endpoints:
- `GET /tasks/{id}/viewer` — viewer status (CDP URL, takeover state, stats)
- `POST /tasks/{id}/human_takeover` — pause agent for human interaction
- `POST /tasks/{id}/resume` — resume agent after human takeover
- `GET /tasks/{id}/screenshot` — current page screenshot (base64 data URL)
- `WS /tasks/{id}/stream` — real-time browser state stream

Config: `SBA_ENABLE_VIEWER`, `SBA_VIEWER_CDP_PORT`,
`SBA_VIEWER_STREAM_SCREENSHOTS`, `SBA_VIEWER_SCREENSHOT_QUALITY`,
`SBA_VIEWER_TAKEOVER_TIMEOUT_S`, `SBA_VIEWER_FORCE_HEADED`.

### Consequences

- The SERA chat UI can show the browser in real-time via WebSocket streaming.
- The human can take control at any point (e.g. to complete a CAPTCHA, log
  in, or verify a result).
- CDP remote debugging gives full interactive access for advanced use cases
  (DevTools, custom inspector).
- The viewer is opt-in (`SBA_ENABLE_VIEWER=false` by default) — headless mode
  remains the default for CI/batch tasks.
- 248 tests passing (225 existing + 23 new viewer tests).
