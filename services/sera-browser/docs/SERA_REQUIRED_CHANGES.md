# SERA Required Changes — Implementation Contract for the SERA Coding Agent

> **Context.** The sera-browser package completed the
> browser-intelligence-v4 rebuild on branch
> `rebuild/browser-intelligence-v4`. SERA's repository is READ-ONLY for
> the browser-intelligence work; the changes below are to be implemented
> by a separate SERA coding agent, against this exact contract. Nothing
> here requires guessing: every interface, schema, and file path below is
> pinned by tests in the sera-browser repo.
>
> **Priority order**: §1–§4 are REQUIRED (correctness + value). §5–§8 are
> RECOMMENDED (value amplification). §0 is a compatibility guard to run
> FIRST.

---

## 0. Compatibility guard (run before anything else)

SERA's import surface is a hard contract and is preserved. Verify:

```bash
python - <<'EOF'
from sera_browser import (
    BrowserManager, ChallengeDetector, ContextManager, Inspector,
    NetworkRecorder, PlaywrightDriver, SessionManager, Settings,
    TaskCreate, ElementResolver,
)
from sera_browser.cancel import CancellationManager
import sera_browser
assert sera_browser.__version__ >= "4.0.0"
print("compat OK", len(sera_browser.__all__), "exports")
EOF
```

Construction signatures that MUST keep working (they do — pinned by
tests/test_kernel_runtime.py + test_element_registry.py):

```python
PlaywrightDriver(settings=…, cancellation=…)
Inspector(driver=…)                 # registry auto-created internally
ElementResolver(page=…)
NetworkRecorder(settings=…, cancellation=…).attach(page)
```

**The one breaking detail**: element `ref` formats changed. See §3.

---

## 1. REQUIRED — ref namespace migration (correctness)

**Problem**: any SERA code that stored, matched, or re-used sera-browser
element references in the v3 formats is now dead:

- `button#3:Login` (driver format) — GONE
- `link7`, `link42` (inspector format) — GONE
- `form1_text_email` (form-field format) — GONE
- `button_submit_2` (synthesized submit refs) — GONE

**The single authoritative namespace** is the ElementRegistry:

```
ref := "e" + digits           (e.g. "e017"; regex ^e\d{3,}$)
```

**Where SERA touches refs today** (`rg -n "ref" backend/app/core/sera_browser_engine.py`):

| Location | v3 assumption | Required change |
|---|---|---|
| `sera_browser_engine.py` session tools that return element lists | refs are opaque strings | No change needed (refs stay opaque) — but NEVER construct selectors from them |
| `sera_browser_engine.py` `resolver.resolve(selector=…)` calls (lines ~1389, ~1406) | CSS selector strings | Keep — `ElementResolver.resolve(selector=…)` still works unchanged |
| Any persisted element refs in assessment memory | v3 formats | One-time migration: drop stale refs; they are per-task artifacts and were never durable anyway |

**Rule for new SERA code**: treat refs as OPAQUE per-task tokens. To act
on an element, pass the ref back to sera-browser (never build selectors).
Refs are only valid within the task/session that produced them.

## 2. REQUIRED — consume the v4 report sections

`AgentReport` gained additive sections (all optional — old readers are
unaffected). SERA should read them wherever the report is consumed:

### 2a. `backend/app/agents/browser.py :: _browser_analyze` (~line 315)

The compact view built for the orchestrator LLM should surface the new
intelligence:

```python
compact = {
    # … existing keys unchanged …
    "coverage": report.get("coverage"),              # NEW: exploration completeness
    "auth_state": (report.get("auth_states") or {}).get("state"),   # NEW
    "blocked_targets": (report.get("blocked_targets") or [])[:10],  # NEW
    "observed_workflows": (report.get("app_graph") or {}).get("workflows", [])[:10],  # NEW
}
```

### 2b. `backend/app/core/sera_browser_engine.py :: _persist_report` (~line 1641)

`browser_analysis.json` already serializes the whole report (v4 sections
included — no change needed). ADDITIONALLY copy the machine-readable run
package into the workspace for specialists:

```python
# after writing browser_analysis.json:
src = Path(session_root_of_task) / "run_package.json"   # written by sera-browser
if src.exists():
    shutil.copy(src, d / "browser_run_package.json")
```

The run package (schema `sera-browser.run-package/v1`) carries
application/routes/workflows/endpoints/auth-states/network/storage/
evidence/coverage WITHOUT any Playwright knowledge — ideal for specialist
consumption. See `docs/SERA-INTEGRATION.md` §5 for the full shape.

### 2c. Auth verdict correction

`report.authentication.detected` means "an auth surface exists" — it does
NOT mean authenticated. The authoritative verdict:

```python
auth = report.get("auth_states") or {}
is_authenticated = bool(auth.get("authenticated"))     # evidence-based only
state_name = auth.get("state")                         # ANONYMOUS|LOGIN_ATTEMPT|AUTHENTICATED|...
```

Fix any SERA display/logic that treats `authentication.detected` as a
logged-in session.

## 3. REQUIRED — evidence ingestion (specialist fuel)

`report.evidence` is the per-step structured ledger (action, args
redacted, classified failure, url before/after, attributed network
requests, screenshot path, duration). Failure kinds are a stable
vocabulary:

```
unknown_ref | stale_ref | not_found | timeout | not_actionable | detached |
nav_rejected | provider_error | browser_disconnected | exception
```

**Dispatcher change** (`backend/app/agents/browser.py` / orchestrator):
when `browser_analyze` returns, forward `report.evidence` entries whose
`attributed_requests` are non-empty into the existing network-recording
path — they are ALREADY endpoint-shaped:

```python
for ev in (report.get("evidence") or []):
    for req in ev.get("attributed_requests", []):
        # req: {"method", "url", "status", "resource_type", "graphql"}
        …  # same HttpRequest row creation as _record_report_network()
```

This upgrades `_record_report_network` from "summary endpoints only" to
"action-attributed traffic" — the Phase-8 correlation SERA's
business-logic and API-discovery specialists can reason over.

## 4. REQUIRED — credential flow for the verified login attempt

v4 supports ONE login attempt per task with postcondition verification,
gated on credentials passed via `TaskCreate.auth`:

```python
rec = await sm.create(TaskCreate(
    target=url, task="…",
    auth={"username": username, "password": password},   # keys: username/password
    …
))
```

**SERA change**: where the assessment has scoped credentials (see
`backend/app/core/credential_scope.py`), pass them through
`analyze_site(..., auth=...)` → `TaskCreate(auth=…)`. The engine function
signature gains an optional `auth: dict | None = None` parameter threaded
into the TaskCreate call (currently dropped). The auth STATE MACHINE —
not the attempt — decides the outcome; the report's `auth_states`
section carries the evidence trail.

## 5. RECOMMENDED — AttackSurfaceGraph enrichment from the run package

New enrichment calls (model the code on existing `enrich_forms` /
`enrich_tech` in `backend/app/core/attack_surface.py`):

```python
async def enrich_browser_run(g, pkg: dict, source: str = "sera_browser") -> None:
    """Ingest a sera-browser run package into the attack surface graph."""
    app = pkg.get("application") or {}
    await enrich_tech(g, pkg.get("target", ""), app.get("technologies", []), source=source)
    for route in app.get("routes", []):
        await enrich_url(g, urljoin(pkg.get("target", "") + "/", route), source=source)
    for form in app.get("forms", []):
        await enrich_forms(g, pkg.get("target", ""), [form], source=source)
    for endpoint in pkg.get("endpoints", []):
        await enrich_api_spec(g, endpoint, source=source)   # or a lighter add_edge
    for wf in pkg.get("workflows", []):
        # UI→API correlations are first-class workflow evidence
        for ep in wf.get("endpoints", []):
            g.add_edge(wf.get("route") or "ui", ep, kind="workflow", source=source)
```

Call site: `analyze_site` after `_persist_report` (§2b).

## 6. RECOMMENDED — DB model extension

`HttpRequest` rows (see `_record_report_network`) can now carry the
action attribution:

```python
# app/db/models.py HttpRequest — additive columns:
source_action: Mapped[Optional[str]]      # e.g. "click"
source_ref:    Mapped[Optional[str]]      # e.g. "e012" (opaque token)
```

Backfill only for new rows (v4 evidence); no migration of old data.

## 7. RECOMMENDED — lifecycle events for the dispatcher

`analyze_site` already emits agent status via `_emit_agent_status`. v4
report sections give the dispatcher richer end-of-task events:

```python
_emit_agent_status(aid, "finished", url=url, message=(
    f"browser analysis: {report['status']}; coverage "
    f"{report['coverage']['routes']['visited']}/{report['coverage']['routes']['discovered']} "
    f"routes, {len(report.get('apis') or [])} APIs, auth={auth_state}"
))
```

Also surface `blocked_targets` in the message when non-empty — the
orchestrator LLM should know which targets the browser engine abandoned
(and why: loop/failure), so it does not re-request them.

## 8. RECOMMENDED — UI/data requirements

Frontend types (`frontend/src/types/*.ts`) gain optional fields on the
browser analysis view (all additive, all optional):

```ts
interface BrowserAnalysis {
  // … existing fields unchanged …
  coverage?: {
    routes:    { discovered: number; visited: number; blocked: number; percent: number };
    elements:  { discovered: number; interacted: number; blocked: number; percent: number };
    forms:     { discovered: number; inspected: number; percent: number };
    endpoints: { discovered: number };
    auth_states: { discovered: string[]; reached: string[] };
  };
  auth_states?: { state: string; authenticated: boolean; states_reached: string[] };
  blocked_targets?: string[];
}
```

Suggested UI: a coverage progress bar on the assessment's
"understanding" phase (routes visited/discovered), and the auth state
chip replacing any boolean "auth detected" indicator.

---

## Verification checklist for the SERA coding agent

1. [ ] Compat guard (§0) passes on the installed sera-browser v4.
2. [ ] `rg -n "button#|link[0-9]|form[0-9]_|button_submit" backend/` finds no
      ref-format assumptions in SERA code (refs treated as opaque).
3. [ ] `_browser_analyze` compact view includes coverage/auth_state.
4. [ ] `browser_run_package.json` lands in the workspace next to
      `browser_analysis.json`.
5. [ ] Evidence-attributed HttpRequest rows appear for a fresh assessment.
6. [ ] Credential-scoped assessments pass `auth=` through to TaskCreate.
7. [ ] No SERA code path treats `authentication.detected` as
      "authenticated session".
8. [ ] Frontend compiles with the additive optional fields.

The sera-browser side is fully pinned by its own test suite (305 tests,
including the failure-injection matrix and the Juice Shop v4 acceptance);
SERA-side changes can be developed against
`pip install "sera-browser @ git+https://github.com/mir-ashiq/sera-browser@rebuild/browser-intelligence-v4"`.
