# SERA Browser Agent — Browser Automation Manual

This document is the operational manual: how the agent drives the browser,
how state is maintained, how primitives behave, how challenges are handled,
how to run tests, how to run the Juice Shop demo, and how SERA will integrate
later.

## 1. Running the API

```bash
cd sera-browser-agent
pip install -e .
python -m sera_browser_agent             # serves on 127.0.0.1:8765
# or
uvicorn sera_browser_agent.api.app:app --host 127.0.0.1 --port 8765
```

Configuration is environment-driven (see `.env.example`):

```
SBA_BROWSER_MODE=auto            # auto | headed | headless
SBA_DEFAULT_TIMEOUT_MS=30000
SBA_NAVIGATION_TIMEOUT_MS=45000
SBA_NETWORK_IDLE_TIMEOUT_MS=8000
SBA_MAX_STEPS=40
SBA_LOCAL_LAB=false              # set true for authorized lab targets
SBA_ALLOWED_DOMAINS=             # comma-separated allowlist
SBA_ALLOWED_PRIVATE_TARGETS=     # comma-separated host:port for local_lab
SBA_ENABLE_BROWSER_USE_FALLBACK=false
# LLM planner (optional)
SBA_LLM_PROVIDER=none            # none | zai
SBA_LLM_TIMEOUT_S=30
SBA_LLM_MAX_CONSECUTIVE_FALLBACKS=5
SBA_SENTRY_DSN=                  # optional
```

### LLM planner (provider-agnostic)

The deterministic `TaskPlanner` is always available as the default and as
the fallback. To enable LLM-backed reasoning, set the provider and
credentials via env vars (or `.env`):

```
SBA_LLM_PROVIDER=zai          # none | zai | openai
SBA_LLM_MODEL=glm-4-plus       # provider-specific model name
SBA_LLM_API_KEY=               # required for openai; ignored by zai
SBA_LLM_BASE_URL=              # optional; defaults to OpenAI's endpoint
SBA_LLM_TIMEOUT_S=60
SBA_LLM_VISION_CAPABLE=false   # set true if the model accepts images
SBA_LLM_MAX_CONSECUTIVE_FALLBACKS=5
```

**Supported providers:**

| `SBA_LLM_PROVIDER` | Implementation | Works with |
|---|---|---|
| `none` (default) | `_NoneLLMClient` (always unavailable → deterministic) | — |
| `zai` | `ZaiLLMClient` (z-ai CLI subprocess) | Z.ai GLM models |
| `openai` | `OpenAICompatLLMClient` (OpenAI REST API via httpx) | OpenAI, Azure OpenAI, OpenRouter, Together, Anyscale, vLLM, Ollama, LM Studio |

**Provider examples:**

```bash
# OpenAI
SBA_LLM_PROVIDER=openai
SBA_LLM_MODEL=gpt-4o-mini
SBA_LLM_API_KEY=sk-...
SBA_LLM_BASE_URL=https://api.openai.com/v1
SBA_LLM_VISION_CAPABLE=true

# Azure OpenAI
SBA_LLM_PROVIDER=openai
SBA_LLM_MODEL=my-deployment-name
SBA_LLM_API_KEY=...
SBA_LLM_BASE_URL=https://my-resource.openai.azure.com/v1

# OpenRouter (access many models via one API)
SBA_LLM_PROVIDER=openai
SBA_LLM_MODEL=anthropic/claude-3.5-sonnet
SBA_LLM_API_KEY=sk-or-...
SBA_LLM_BASE_URL=https://openrouter.ai/api/v1

# Ollama (local model)
SBA_LLM_PROVIDER=openai
SBA_LLM_MODEL=llama3.2
SBA_LLM_BASE_URL=http://127.0.0.1:11434/v1
SBA_LLM_API_KEY=ollama   # Ollama ignores the key but the client requires one

# Z.ai CLI
SBA_LLM_PROVIDER=zai
SBA_LLM_MODEL=glm-4-plus
```

The Browser Agent architecture does NOT hardcode any provider — the
`make_llm_client(config)` factory selects the implementation by
`SBA_LLM_PROVIDER`. Adding a new provider only requires implementing the
`LLMClient` ABC and adding one branch to the factory.

When enabled, the `LLMTaskPlanner`:

1. builds a compact JSON view of `BrowserState` (no credentials, ≤12KB)
2. calls `LLMClient.complete()` with a strict system prompt
3. parses the LLM's JSON response (`{thinking, action, args, reason}`)
4. validates the action against the current state (refs must exist,
   `navigate` must pass SSRF validation, action must be in the allowed set)
5. hands the validated `Step` to `ActionExecutor` — the LLM never touches
   Playwright directly

**Error classification** (all subclasses of `LLMError`):
- `LLMUnavailableError` — provider not installed / not configured / 5xx
- `LLMTimeoutError` — call exceeded `SBA_LLM_TIMEOUT_S`
- `LLMAuthError` — 401 / 403 (bad API key)
- `LLMRateLimitError` — 429 (quota exceeded)
- `LLMResponseError` — malformed / empty response

Fallback to the deterministic planner is automatic on any of the above.

**Vision is optional and never sent on every step.** The planner only
escalates to vision when:
1. loop recovery triggers (semantic repetition detected), AND
2. `SBA_LLM_VISION_CAPABLE=true` (the client supports vision), AND
3. a screenshot provider is configured (the agent wires this automatically).

LLM planner stats are recorded as an observation in the final report:
`llm_planner: calls=N succeeded=N fallbacks=N invalid=N vision_calls=N
vision_succeeded=N`.

## 2. Creating a Task

```bash
curl -X POST http://127.0.0.1:8765/tasks \
  -H 'content-type: application/json' \
  -d '{
    "target": "http://localhost:3000",
    "task": "Analyze this application for business flows, authentication, APIs, routes, technologies and security-relevant browser behavior.",
    "browser_mode": "headless",
    "timeout": 180,
    "local_lab": true,
    "allowed_targets": ["localhost:3000", "127.0.0.1:3000"]
  }'
```

Returns `{"task_id": "...", "status": "running"}`.

## 3. Polling State & Result

```bash
curl http://127.0.0.1:8765/tasks/<id>
curl http://127.0.0.1:8765/tasks/<id>/state
curl http://127.0.0.1:8765/tasks/<id>/result
```

## 4. Cancelling

```bash
curl -X POST http://127.0.0.1:8765/tasks/<id>/cancel
```

Cancellation is cooperative and immediate; `force_cleanup` runs before the
task is marked finished. No leaked Chromium processes should remain.

## 5. Playwright Primitives (behavior contract)

| Primitive            | Behavior                                                                                |
|----------------------|-----------------------------------------------------------------------------------------|
| `navigate(url)`      | Idempotent: if `url == current_url` and `reload` not requested, only re-snapshots.      |
| `back/forward`       | History-aware; falls back to current state if no history.                               |
| `reload`             | Forces a fresh navigation.                                                              |
| `snapshot()`         | Builds accessibility tree + DOM ref map; updates `BrowserState.snapshot`/`dom_hash`.    |
| `find_elements`      | By role/name/text/selector — selectors are validated against the snapshot.             |
| `click(ref|selector)`| Resolves ref → element → `locator.click()` with bounded retry + visibility check.      |
| `fill`/`type`        | Clears then types; supports `delay` per char.                                           |
| `select`             | `<select>` aware; accepts value/label/index.                                            |
| `checkbox/radio`     | State-aware (no double-toggle).                                                         |
| `keyboard`/`mouse`   | Low-level; used for escape hatches.                                                     |
| `hover`/`scroll`     | Visibility-aware; scroll supports element/coords/`bottom`/`top`.                        |
| `screenshot`         | On-demand; full page or viewport; stored under `session_dir/screenshots/`.             |
| `tabs/popups`        | Tracked via context event; new pages added to the session page set.                    |
| `cookies`            | Read/write via context; never logged.                                                   |
| `localStorage`/`ss`  | Read keys + selected values (values redactable).                                        |
| `eval_js`            | Gated behind `allow_js_eval` flag (default off for untrusted targets).                  |

## 6. State Maintenance

`BrowserState` is updated incrementally:

- After every `navigate`/`back`/`forward`/`reload` → `current_url`, `title`,
  `navigation_history`, `page_type`, `timings.navigation`.
- After every `snapshot()` → `interactive_elements`, `forms`, `links`,
  `dom_hash`.
- Continuously from event listeners → `requests`, `responses`,
  `console_errors`, `page_errors`, `websockets`, `downloads`.
- After auth detection → `authenticated`, `user_role`.

The agent reasons from `BrowserState` instead of re-scraping on every step.

## 7. Challenge Handling — Authorized Testing Only

`ChallengeDetector` runs after each navigation + snapshot. Detected challenges
are appended to `BrowserState.challenges` with `type`, `evidence`, and
`handled` fields.

### Official test mechanisms (use these for automated testing)

- **Cloudflare Turnstile** — use the documented **test keys** (sitekey
  `1x00000000000000000000AA` = always-pass, secret `1x0000000000000000000000000000000AA`).
  Configure your test site with these keys; the agent will detect a Turnstile
  widget, recognize the test-key flow, and proceed. Never deploy test keys in
  production.
- **reCAPTCHA** — Google publishes test keys for development
  (site key `6LeIxAcTAAAAAJcZVRqyebecYbd0OXwI1eAO7TNJ`, always-pass). Use only
  on owned test pages.
- **hCaptcha** — provides a test mode via `HCAPTCHA_TESTING` env var on
  self-hosted hCaptcha; for owned test sites only.

For challenges that have no official test mode on an **owned** target, the
agent pauses and emits a `human_in_loop_required` event. The HTTP API exposes
`POST /tasks/{id}/resume` (planned) — currently human interaction is done by
attaching to the headed session.

The agent **does not** bypass third-party CAPTCHA / anti-bot protections on
targets you do not own or are not explicitly authorized to test.

## 8. Local Lab Mode

`local_lab=true` allows private-network targets but **only** those listed in
`SBA_ALLOWED_PRIVATE_TARGETS` / `allowed_targets`. SSRF protection remains on
for everything else.

## 9. Running Tests

```bash
cd sera-browser-agent
pip install -e ".[test]"
playwright install chromium
pytest -q
```

Tests use a small local FastAPI site (`tests/fixtures/local_site/`) that
exposes forms, SPA navigation, WebSocket echo, downloads, console errors, and
a fake challenge page. Juice Shop tests are marked
`@pytest.mark.juice_shop` and skipped automatically when Juice Shop is not
running.

## 10. Juice Shop Demo

```bash
# Terminal 1 — start Juice Shop (requires Node)
cd scripts
./run_juice_shop_demo.sh        # or: npm install -g juice-shop && juice-shop
# Terminal 2 — run the agent
python scripts/run_juice_shop_demo.py --target http://localhost:3000
```

The demo:

1. starts a Browser Agent task against Juice Shop,
2. navigates, inspects the accessibility tree, identifies application type,
3. discovers routes (SPA), forms (login/search/feedback), API endpoints
   (`/api/*`, `/rest/*`),
4. records network traffic (REST + GraphQL + WebSocket),
5. explores a meaningful workflow (browsing products / search),
6. returns a structured report saved under `download/`,
7. cleanly shuts the browser down.

## 11. Cancellation Demo

```bash
python scripts/demo_cancellation.py --target http://localhost:3000
```

Starts a long task, cancels it after 3 seconds, then verifies no `chromium`
process remains.

## 12. SERA Integration (future)

SERA will POST to `/tasks` with a high-level objective. The agent returns a
deterministic structured report at `/tasks/{id}/result`. SERA does not need
any Playwright knowledge. The integration itself is intentionally **not**
implemented here — only the contract.

## 13. What the Agent Will NOT Do

- Will not execute arbitrary host shell commands.
- Will not disable SSRF protection globally.
- Will not silently access unrelated private networks.
- Will not log credentials.
- Will not share browser state between assessments.
- Will not let one task control another task's browser.
- Will not blindly trust LLM-generated selectors.
- Will not bypass third-party CAPTCHA / anti-bot protections.

## 14. Real-View Browser (Human-Takeoverable, for SERA Chat UI)

The "real view, not a relay" paradigm: the browser is a **native visible
window** the human can see and interact with, not a headless relay.

### Enabling

```bash
SBA_ENABLE_VIEWER=true
SBA_VIEWER_CDP_PORT=9222
```

When enabled, the browser:
- Launches in **headed mode** (overrides `SBA_BROWSER_MODE`)
- Adds `--remote-debugging-port=9222` for CDP access
- Streams screenshots + state to WebSocket clients after each step

### How it works

1. **CDP remote debugging** — the SERA UI can connect to
   `http://127.0.0.1:9222` for full interactive access (DevTools, custom
   inspector). The CDP URL is exposed in `GET /tasks/{id}/viewer`.

2. **Screenshot + state streaming** — after each agent step, a JPEG screenshot
   + current URL/title/step info is sent to all connected WebSocket clients
   via `WS /tasks/{id}/stream`. The SERA chat UI renders this stream.

3. **Human takeover** — the agent pauses between steps when a takeover is
   requested. The human interacts with the visible browser window, then
   signals "done". The agent re-observes and continues.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/tasks/{id}/viewer` | Viewer status (CDP URL, takeover state, stats) |
| POST | `/tasks/{id}/human_takeover` | Pause agent for human interaction |
| POST | `/tasks/{id}/resume` | Resume agent after human takeover |
| GET | `/tasks/{id}/screenshot` | Current page screenshot (base64 data URL) |
| WS | `/tasks/{id}/stream` | Real-time browser state stream (screenshots + steps) |

### Human takeover flow

1. The SERA chat UI calls `POST /tasks/{id}/human_takeover?reason=login`.
2. The agent pauses at the next step check (between actions).
3. `GET /tasks/{id}/viewer` returns `takeover_active: true`.
4. The human interacts with the visible browser window (logs in, solves
   CAPTCHA, verifies result, etc.).
5. The SERA chat UI calls `POST /tasks/{id}/resume`.
6. The agent re-observes the page state and continues the task.

### SERA chat UI integration (JavaScript)

```javascript
// Connect to the real-time stream
const ws = new WebSocket(`ws://localhost:8765/tasks/${taskId}/stream`);
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === "step") {
    // data.data.screenshot is a base64 JPEG data URL
    document.getElementById("browser-view").src = data.data.screenshot;
    document.getElementById("step-info").textContent =
      `Step ${data.data.step}: ${data.data.action} → ${data.data.url}`;
  }
  if (data.type === "takeover") {
    // Show "Agent paused — human can interact" banner
    showBanner(data.data.message);
  }
  if (data.type === "resume") {
    hideBanner();
  }
};

// Request human takeover
await fetch(`http://localhost:8765/tasks/${taskId}/human_takeover`, { method: "POST" });

// Resume after the human is done
await fetch(`http://localhost:8765/tasks/${taskId}/resume`, { method: "POST" });
```

### CDP direct access

The SERA UI can connect directly to the browser via CDP for full interactive
control (DevTools, custom inspector, etc.):

```
http://127.0.0.1:9222  (exposed in GET /tasks/{id}/viewer as cdp_url)
```

### Configuration

| Env var | Default | Description |
|---|---|---|
| `SBA_ENABLE_VIEWER` | `false` | Enable the real-view browser viewer |
| `SBA_VIEWER_CDP_PORT` | `9222` | CDP remote debugging port |
| `SBA_VIEWER_STREAM_SCREENSHOTS` | `true` | Stream JPEG screenshots after each step |
| `SBA_VIEWER_SCREENSHOT_QUALITY` | `60` | JPEG quality (1-100) |
| `SBA_VIEWER_SCREENSHOT_MAX_WIDTH` | `1280` | Screenshot max width (px) |
| `SBA_VIEWER_TAKEOVER_TIMEOUT_S` | `300` | How long the agent waits for the human |
| `SBA_VIEWER_FORCE_HEADED` | `true` | Force headed mode when viewer is enabled |
