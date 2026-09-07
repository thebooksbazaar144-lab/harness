# SERA Browser Agent

A standalone, production-quality **Playwright-first** autonomous browser agent.
Receives a high-level objective, drives a real browser, reasons from a single
structured browser state, escalates to vision only when needed, and returns a
deterministic structured report.

Designed as a reusable worker that SERA can call without any Playwright
knowledge. Now supports a **real-view, human-takeoverable browser** for the
SERA assessment chat UI.

> Status: standalone. The SERA integration itself is **not** implemented in
> this repository — only the contract (see `docs/ARCHITECTURE.md` §17).

## Highlights

- **Playwright first.** Direct async Playwright is the normal path.
  Accessibility-tree/snapshot-driven interaction with deterministic element
  refs (Playwright-MCP-inspired). No MCP runtime dependency.
- **Reason-from-state.** One structured `BrowserState` per task; no
  re-scraping on every step.
- **LLM planner with provider-agnostic layer + deterministic fallback.**
  Optional LLM-backed planner (`SBA_LLM_PROVIDER`) receives a structured
  state view, returns a validated action from a fixed set, and the
  `ActionExecutor` executes it. The LLM never touches Playwright directly.
  Supports Z.ai (`zai`), any OpenAI-compatible API (`openai` — OpenAI,
  Azure, OpenRouter, Ollama, vLLM, ...), or `none` (deterministic only).
  Falls back to the deterministic planner on unavailable / timeout / invalid
  action / cancellation.
- **Vision is optional, not default.** Screenshots are only sent to the LLM
  on the loop-recovery escalation path, never on every step.
- **Idempotent `navigate`.** If the URL is already current, the agent only
  re-snapshots (no reload) unless reload is explicit.
- **Bounded everything.** Timeouts, cancellation, and bounded retries on every
  operation.
- **Loop detection.** Tool repetition + semantic (dom_hash) repetition, with
  recovery or declared block.
- **Network intelligence.** Structured request/response/WebSocket recording
  with sensitive-header redaction. Reusable as a security sensor for SERA.
- **Challenge-aware.** Detects CAPTCHA / Turnstile / Cloudflare / hCaptcha /
  reCAPTCHA / MFA. Uses official test keys on owned test sites; pauses for
  human-in-the-loop otherwise. **Never bypasses** third-party protections.
- **Optional `browser-use` fallback.** Lazy-imported, off by default, never
  the normal path, shares the same state/session/cancellation/result format.
- **Browser isolation.** Per-task isolated context. `cancel_task` +
  `force_cleanup` guarantee no leaked Chromium / context / page / WebSocket.
- **Security boundaries.** SSRF on by default; explicit `local_lab` +
  allowlist for private targets; no credential logging; selectors validated
  against the snapshot.
- **Real-view browser (human-takeoverable).** When `SBA_ENABLE_VIEWER=true`,
  the browser launches in headed mode with CDP remote debugging. The SERA
  chat UI connects via WebSocket to watch every step in real-time
  (screenshots + state). The human can grab control at any time via
  `POST /tasks/{id}/human_takeover` — the agent pauses, the human interacts,
  then `POST /tasks/{id}/resume` continues.

## Quick start

```bash
pip install sera-browser
playwright install chromium
sera-browser             # API on 127.0.0.1:8765
```

Or for development:

```bash
git clone https://github.com/mir-ashiq/sera-browser.git
cd sera-browser
pip install -e ".[dev]"
playwright install chromium
python -m sera_browser             # API on 127.0.0.1:8765
```

Create a task:

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

## Real-View Browser (Human-Takeoverable, for SERA Chat UI)

The "real view, not a relay" paradigm: the browser is a **native visible
window** the human can see and interact with, not a headless relay. The agent
streams every step to the SERA chat UI in real-time, and the human can grab
control at any time.

### Enable

```bash
SBA_ENABLE_VIEWER=true
SBA_VIEWER_CDP_PORT=9222
# The browser will launch in headed mode with CDP remote debugging.
```

### How it works

1. **CDP remote debugging** — Chromium launches with `--remote-debugging-port=9222`.
   The SERA UI (or `chrome://inspect`) can connect to the same browser instance
   for full interactive access.

2. **Screenshot + state streaming** — after each agent step, a JPEG screenshot +
   current URL/title/step info is streamed to connected WebSocket clients.
   The SERA chat UI renders this stream for real-time visibility.

3. **Human takeover** — the agent (or the human via API) can trigger a takeover.
   The agent pauses, the human interacts with the real browser window, then
   signals "done". The agent re-observes and continues.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/tasks/{id}/viewer` | Viewer status (CDP URL, takeover state, stats) |
| POST | `/tasks/{id}/human_takeover` | Pause agent for human interaction |
| POST | `/tasks/{id}/resume` | Resume agent after human takeover |
| GET | `/tasks/{id}/screenshot` | Current page screenshot (base64 data URL) |
| WS | `/tasks/{id}/stream` | Real-time browser state stream |

### SERA chat UI integration

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
  }
  if (data.type === "resume") {
    // Hide the banner
  }
};

// Request human takeover
fetch(`http://localhost:8765/tasks/${taskId}/human_takeover`, { method: "POST" });

// Resume after the human is done
fetch(`http://localhost:8765/tasks/${taskId}/resume`, { method: "POST" });
```

### CDP direct access

The SERA UI can also connect directly to the browser via CDP for full
interactive control (DevTools, custom inspector, etc.):

```
http://127.0.0.1:9222  (the CDP endpoint — exposed in /tasks/{id}/viewer)
```

## Documentation

- `docs/ARCHITECTURE.md` — component map, flow, integration contract.
- `docs/BROWSER-AUTOMATION.md` — operational manual, primitives, challenges.
- `docs/DECISIONS.md` — ADR log.
- `docs/RELEASE.md` — release process, PyPI Trusted Publishing, versioning.
- `docs/SERA-INTEGRATION.md` — SERA integration contract (library + HTTP modes).

## Installation

```bash
# From PyPI (public):
pip install sera-browser

# With optional extras:
pip install "sera-browser[fallback,vision,dev]"
```

The package is **public on PyPI**; the source repository is **private on GitHub**.
PyPI publishing uses Trusted Publishing (OIDC, no stored tokens).

## License

Apache-2.0.
