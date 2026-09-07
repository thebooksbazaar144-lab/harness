# SERA Browser — Architecture Audit

**Repo:** `mir-ashiq/sera-browser` · **Audited state:** `main @ 69d78d6` (= tag `v3.1.1`, also frozen as `sera-browser-v3.1.1-legacy`)
**Branch:** `rebuild/browser-intelligence-v4` · **Audit date:** 2026-09-04
**Scope:** full source tree — 48 modules, 11,607 LOC under `src/`, 19 test files, docs, packaging, CI. SERA repo (`mir-ashiq/SERA`, read-only) cross-referenced for the integration contract.

> This is the Phase-1 deliverable of the browser-intelligence-v4 rebuild plan. **No production code was changed in this commit.** It contains only this audit and the file-level rebuild map (Phases 2–19). Every claim below is backed by a `file:line` reference verified against the audited tree.

---

## 1. Executive summary

The repository contains **two architectures**: the one described in `docs/ARCHITECTURE.md` (versioned refs, multi-strategy resolution, self-healing, bounded everything) and the one actually wired into the execution path (positional string refs, blind double-path locators, no retry budgets, no result feedback). The described architecture exists as **exported-but-never-called code** (`refmap.py`, `resolver.py`, `som.py`, `trimmer.py`, `browser_use_fallback.py`). The wired architecture produces every failure you have observed:

| Observed behavior | Root cause (verified) | Where |
|---|---|---|
| A click burns ~30s, fails, then is retried identically | 15s selector path **+** 15s role/name fallback path per click; no retry budget; `tool_repeat_limit=4` permits 4 identical retries → up to ~2 min per stuck element | `driver.py:535-567`, `config.py:36,43` |
| `loop → vision → loop → vision` until step budget dies | On semantic loop with a vision-capable client, vision failure returns `None` → plain `screenshot` step → re-observe records the *same* `dom_hash` → semantic loop still true → vision again. `reset_dom_history()` is only called for **non-vision** clients | `llm_planner.py:273-307`, `loop.py:50-62`, `agent.py:640` |
| `ref not found` / clicks hitting the wrong element | Three incompatible ref namespaces; resolution ignores the positional index (`.first` on role+name); system prompt tells the LLM a ref is "an integer index" while refs are actually `button#1:Login` strings | `driver.py:451`, `inspector.py:105,156,119-126`, `driver.py:541-567`, `llm_planner.py:122-124` |
| "authenticated" conclusions with no evidence | `attempt_safe_login` + page no longer classified "login" ⇒ `mark_authenticated()` — the exact "clicked Login = authenticated" fallacy | `agent.py:412-414`, `state.py:104-109` |
| The LLM extracts nothing useful | `extract` is a **fake success** (records the request, does nothing); `scrape`/`content` results are reduced to count-strings ("scraped 15 items") and the data is discarded before the LLM ever sees it | `executor.py:171-200`, `memory.py:70` |
| Coverage stalls after a handful of routes | Deterministic planner hard-caps route exploration at 5; no element/form/workflow coverage model exists at all | `planner.py:152` |
| CI never ran; published CLI broken | `.github/workflows/ci.yml` branch filter is literally `ain]` (corrupted YAML since first packaging commit `926cd91`); `pyproject.toml` points the `sera-browser` console script at a nonexistent `sera_browser_agent` package | `ci.yml:5,7`, `pyproject.toml:85,97,112` |

**The headline conclusion for the rebuild:** Playwright is not the problem — confirmed. The LLM client layer is not the problem — it is the strongest module in the repo. The problem is the **identity/resolution layer** (Phase 4), the **time/retry budget policy** (Phase 5), and the **missing coverage/graph/feedback engines** (Phases 2, 6, 7, 8). The correct skeleton for most of this already exists in the repo as dead code; the rebuild should promote it to the live path and delete the brittle path, not invent from scratch.

**One hard external constraint:** SERA already imports this package (`backend/app/core/sera_browser_engine.py:112-124` in the SERA repo). The symbols `BrowserManager, ChallengeDetector, ContextManager, Inspector, NetworkRecorder, PlaywrightDriver, SessionManager, Settings, TaskCreate, ElementResolver` and `sera_browser.cancel.CancellationManager` **must remain importable at their current paths** for the entire rebuild. Section 6 encodes this as a compatibility contract.

---

## 2. Audit method

1. Read every module under `src/sera_browser/` (48 files), all 5 docs, `pyproject.toml`, `.env.example`, `.github/workflows/*`, the Juice Shop tests, loop/timeout tests, and the demo script.
2. Traced the live execution path end-to-end: `SessionManager.create → BrowserAgent.run → (TaskPlanner | LLMTaskPlanner | PAV).next_step → ActionExecutor.execute → Inspector.find_by_ref → PlaywrightDriver.click_ref/fill/...`.
3. Grep-verified call sites for every "advanced" subsystem (`build_refmap`, `ElementResolver`, `SetOfMarkAnnotator`, `trim_snapshot`, `BrowserUseFallback.attempt`, `llm_max_consecutive_fallbacks`, …) to separate wired code from dead code.
4. Cross-referenced the SERA repo (read-only) to extract the exact import surface SERA depends on, and the sinks it offers for discovery data (`AttackSurfaceGraph.enrich_*`, `HttpRequest`, finding evidence, `analyze_site` report keys).
5. Derived the rebuild map (Section 8) from the failure map, not from generic best practice.

---

## 3. Verified failure map (symptom → cause → file:line)

### F1 — The 30-second click (P0 · rebuild Phase 5)

```
click(ref)
 ├─ path A: element.selector hint (text=/has-text/a[href])  → loc.click(timeout=15_000)
 └─ path B (only if A failed): get_by_role(role,name).first → loc.click(timeout=15_000)
```

- `PlaywrightDriver.click_ref` (`driver.py:535-567`): tries the selector hint first; on any `PlaywrightTimeoutError/PlaywrightError` it **falls through to the role+name path** — a second, full-length actionability wait. Worst case ≈ 15s + 15s + the `wait_or_cancel` margins (+2s each, `driver.py:546,559`) ≈ **32s** before returning `False`.
- The selector hints themselves are loose substring matchers: `text={name}` / `button:has-text({name!r})` (`driver.py:501-510`) — they can match *multiple* nodes and `.first` silently picks the wrong one.
- `ActionExecutor.execute` has **no timeout policy of its own and no retry accounting** (`executor.py:82-89`); it returns a single `ActionResult` and the agent re-observes (`agent.py:386-397`), leaving the decision to re-click entirely to the planner.
- `tool_repeat_limit = 4` (`config.py:43`) means the loop detector tolerates **four consecutive identical `click(ref)` calls** (`loop.py:41-48`) before suggesting recovery. One stuck element can therefore cost `4 × ~32s ≈ 2 minutes`, plus 4 full re-observations, before anything changes.
- Playwright's own actionability checks (visible/stable/receives events/enabled) are what these 15s waits are *waiting for* — the design wraps Playwright's auto-waiting in *another* blind wait instead of trusting it with a short budget and handling failure. This is precisely the anti-pattern Phase 5 of the rebuild plan calls out.

**Budget policy that must replace it (Phase 5):** `action_timeout_ms = 8_000` (single path), `action_retry_count = 1`, retry only *after* a fresh re-observe and re-resolve, and a per-element failure ledger that invalidates the ref after one failed retry (see F3 for the registry that owns this).

### F2 — `loop → vision → loop → vision` (P0 · rebuild Phases 10/11)

Two independent mechanisms produce it:

**(a) LLM path** (`SBA_LLM_PROVIDER` ≠ none, vision-capable client):
1. `LoopRecoveryManager.semantic_loop` is true (same `dom_hash` 3 observations in a row — `loop.py:50-62`).
2. `LLMTaskPlanner.next_step` → recovery == `"try_alternate_observation"` → because `client.supports_vision` and a screenshot provider is wired, calls `_try_vision_escalation` (`llm_planner.py:281-284`).
3. If the vision call **fails for any reason** (timeout, 429, 5xx, unparseable JSON, rejected action — `llm_planner.py:476-528`), `_try_vision_escalation` returns `None`.
4. Control falls to `llm_planner.py:301-307`: return a plain `screenshot` capture step.
5. Agent executes the screenshot, re-observes, and `_observe` records the **same** `dom_hash` (`agent.py:640`).
6. Semantic loop is still true → goto 2. **The dom-hash history is never reset on this path.** The only `reset_dom_history()` call site is `llm_planner.py:286` — inside the branch for **non-vision** clients (`if not self.client.supports_vision and self.client.available()`).
7. Termination only via `tool_loop` firing on four identical `screenshot()` keys (no ref arg → key degenerates to `"screenshot()"`, `executor.py:63-64`) → LLM anti-repetition redirect (≤3, `llm_planner.py:231`) → then "blocked".

**(b) Deterministic path** (`SBA_LLM_PROVIDER=none` — the **default**):
1. Same semantic loop, but `TaskPlanner.next_step` recovery returns a `screenshot` step every time (`planner.py:96-103`).
2. No `reset_dom_history()` exists anywhere on this path; the planner's only escape is the one-shot `_loop_escapes` (`planner.py:80-95`): 4 screenshot cycles → escape → any second loop → **whole task declared `blocked`** (mission aborted, not "mark failed and continue").

**Fix shape (Phases 10/11):** the LoopEngine must own *termination with prejudice*: on trigger → invalidate target, record failure, mark blocked, refresh observation, choose a *different* target; vision gets a per-state budget (2 attempts) and is then disabled for that state; provider failure must never become the recovery strategy.

### F3 — Element reference chaos (P0 · rebuild Phase 4)

Three ref namespaces coexist and none is sound:

| Namespace | Format | Emitted by | Resolvable? |
|---|---|---|---|
| Snapshot refs | `button#1:dismiss_cookie_message` (role + per-(role,name) counter + name[:40], spaces/slashes → `_`) | `driver.py:445-452` | Yes, via `Inspector.find_by_ref` (`inspector.py:204-208`) against the last snapshot's `interactive_elements` |
| Form-field refs | `form1_input_email_x` | `inspector.py:105-107` | **No** — never present in `interactive_elements`; `validate_action` rejects them (`action_schema.py:385-386`) |
| Link refs | `link7` | `inspector.py:156` | **No** — same reason |
| Fabricated submit refs | `button_submit_1` | `inspector.py:119-126` | **Never** — synthesized *instead of* the real button's actual ref; `find_by_ref` always misses |

Compounding defects:
- **The positional index is discarded at resolution time.** `click_ref`/`fill` resolve via `element.selector` or `get_by_role(role, name).first` (`driver.py:541-567, 569-583`). Two distinct elements `button#1:submit` and `button#2:submit` resolve to the *same* DOM node; a renamed button becomes unresolvable even though a fresh snapshot would show it.
- **Refs are unstable across DOM changes** by construction: the counter depends on tree order (`driver.py:448-451`), so an inserted banner renumbers everything after it.
- **The system prompt lies to the LLM**: "every interactive element in the state view has a `ref` field (an integer index)" (`llm_planner.py:122-124`) while real refs are strings. The model is set up to fail validation.
- **The correct design already exists, unwired**: `refmap.py` implements versioned integer indices, monotonic snapshot versions, content signatures, stale-ref rejection (`resolve_ref`, `refmap.py:253-272`); `resolver.py` implements the ref → css → role+name → text → coordinate ladder with visibility preflight. **Zero call sites in the execution path** (grep verified: only `__init__.py:91-97` re-exports them). SERA imports `ElementResolver` (`sera_browser_engine.py:112-124`) — so the symbol works for external users while the agent itself never uses it.
- The state view truncates to 40 elements (`llm_planner.py:66, 561`) with a 12k-char JSON cap (`llm_planner.py:65`) — on a Juice Shop page with hundreds of interactive elements, the LLM is structurally blind to most of the page, and any ref it can see refers to elements renumbered since the last snapshot.

**Fix shape (Phase 4):** one `ElementRegistry` owning the only ref namespace (`el_017`-style opaque ids), producing Playwright-strict locators (role/name/text/test-id, per Playwright's locator guidance), versioned snapshots, ref invalidation on DOM change, and a hard rule: never store an `ElementHandle` as identity; always re-resolve at action time.

### F4 — Authentication-by-vibes (P1 · rebuild Phase 9)

- `agent.py:412-414`: after an `attempt_safe_login` step, if `state.page_type != "login"` → `mark_authenticated(state)` — no cookie/session/redirect/identity-UI evidence is consulted.
- `_classify_page` (`state.py:63-76`) classifies "login" by URL substrings ("login", "signin", "auth") — SPA route changes without full navigation don't even update this reliably.
- `ReportBuilder` then reports `authentication.detected` purely from "does any form have a password field" (`report.py:28-35`).
- Consequence for SERA: auth-related findings inherit a fabricated premise. Phase 9's evidence-driven state machine (cookie delta, storage delta, redirect target, protected-endpoint success, identity UI) does not exist anywhere in the repo.

### F5 — No action-result feedback channel (P0 · rebuild Phase 2)

The LLM planner chooses each action from a state view, but action **results never return data**:
- `extract`: "the actual extraction is delegated to the planner's LLM client; the executor just records the request" — a guaranteed-success no-op (`executor.py:171-176`).
- `scrape`: returns `f"scraped {count} items"`; the scraped rows are local variables that die with the function (`executor.py:182-188`).
- `content`: returns `f"content {fmt} {len} chars"`; the content itself is discarded (`executor.py:194-200`).
- `search_page`: returns match counts, not matches (`executor.py:270-279`).
- The only residue the LLM ever sees is `ActionResult.detail` (≤200 chars) via `MemoryManager.to_view().recent_steps[].detail` (`memory.py:70, 90-109`).

So the "reason from state" loop is actually "reason from URL + element list + 200-char breadcrumbs". Combined with F3's 40-element cap, the planner is mostly blind. This is the architectural core of Phase 2: observation/extraction results must become first-class structured state fed back to the planner.

### F6 — Coverage & application-model gaps (P1 · rebuild Phases 6/7/8/12)

- Deterministic exploration is bounded to **5 routes** total (`planner.py:152`, `self._route_iter < 5`), irrespective of how many routes were discovered.
- No tracking of visited elements/forms/workflows/endpoints; no blocked-target ledger; no unexplored-queue. `TaskPlanner._explored_routes` (a set) is the entire coverage model.
- `AgentReport.workflows` is a list of **heuristic labels** ("spa-routing", "authentication-form", "rest-api") generated by string-sniffing (`report.py:100-113`) — there is no workflow discovery at all, yet SERA persists these keys verbatim into `understanding/browser_analysis.json`.
- No correlation layer between UI action → route change → network request → endpoint (Phase 7/8). `NetworkRecorder` captures traffic well, but nothing links a click to the `/rest/...` calls it triggered; `merge_network` only copies deques into state (`state.py:79-83`).
- `max_steps = 40` default (`config.py:39`) with each step costing an LLM roundtrip *and* a full observe — a coverage mission against Juice Shop needs an order of magnitude more budget, which is only viable once steps get cheap (deterministic exploration + batch observation).

### F7 — Dead / unwired code inventory (cleanup · affects trust in the codebase)

| Unit | Status | Evidence |
|---|---|---|
| `browser/refmap.py` (versioned refs, stale detection) | **Dead** in agent path; exported for SERA | no call sites for `build_refmap`/`resolve_ref` outside the module |
| `browser/resolver.py` (multi-strategy self-heal) | **Dead** in agent path; SERA imports the symbol | `ElementResolver` never instantiated internally |
| `browser/som.py` (Set-of-Mark vision) | **Dead** | `SetOfMarkAnnotator` no call sites |
| `browser/trimmer.py` (snapshot trimming) | **Dead** | `trim_snapshot` never called despite `snapshot_max_chars` setting |
| `browser/browser_use_fallback.py` | **Dead** in execution | `BrowserUseFallback.attempt` never invoked; only health-endpoint availability check |
| `task/pav.py` (Plan-Act-Validate) | Off by default | `agent_pattern="none"` (`config.py:103`); opt-in only |
| `eval/harness.py` | Standalone dev harness | no integration with tests/CI |
| `Settings.llm_max_consecutive_fallbacks` | **Dead setting** | `config.py:71` defined, never read anywhere — the "LLM fallback safety valve" does not exist at runtime; a dead provider falls back every step forever |
| `"download"` action | **Broken action** | accepted by validator (`action_schema.py:66,117`) but has no executor branch → always "unknown action" (`executor.py:374-376`) |
| `history.replay` | **Broken** | passes `state={}` and `loop=None` placeholders into `executor.execute` (`history.py:157-163`) → `AttributeError` on `loop.record_tool` |
| `snapshot_method` | Default `"dom"` contradicts docs | `config.py:109` vs `docs/ARCHITECTURE.md` §"auto (default)" — the richer CDP AX path is off by default |

Dead code is not neutral: it is why the docs and the behavior disagree, and why every previous "fix" session reasoned about the wrong layer.

### F8 — Packaging & CI defects (hygiene P0, fix before rebuild lands)

- `pyproject.toml:85` — `sera-browser = "sera_browser_agent.__main__:main"`: the package is `sera_browser`, not `sera_browser_agent`. The published 3.1.1 console script **cannot start** (entry-point module not found). Also `pyproject.toml:97` (`package-data` key) and `:112` (coverage `source`) reference the same nonexistent package → coverage measures nothing.
- `.github/workflows/ci.yml:5,7` — `branches: ain]` (corrupted YAML, should be `[main]`). Present since the first packaging commit `926cd91` (2026-08-31) → **CI has never executed on this repository.** Every "green" assumption is unverified.
- Docs claim "public on PyPI; source private on GitHub" (`README.md:184-185`) while `pyproject.toml:88-91` publishes the GitHub URL — minor, but the Trusted Publishing flow published a wheel whose CLI is broken (F8a), so the release pipeline itself needs a smoke test (`sera-browser --help`) as a release gate.

---

## 4. What currently works — RETAIN list

These modules are architecturally sound and should survive the rebuild largely intact (adapt interfaces, don't rewrite):

| Module | Why it's good | Verdict |
|---|---|---|
| `security.py` | SSRF guard with DNS-resolution private-IP check, cloud-metadata always-block, `local_lab` + host/host:port allowlists, suffix domain matching | **Retain as-is** |
| `cancel.py` | Cooperative cancellation with `wait_or_cancel` racing the cancel event; correct task/awaitable cleanup semantics | **Retain as-is** |
| `task/llm_client.py` | Provider-agnostic ABC; clean error taxonomy (`LLMUnavailable/Timeout/Auth/RateLimit/Response`); OpenAI-compatible REST with 401/403/429/5xx classification; bounded timeouts; vision-capability flag; `ZaiLLMClient` subprocess isolation; tolerant JSON parsing | **Retain as-is** — this is the model layer the rebuild plan's "provider failure must not become a strategy" can build on |
| `net/recorder.py` | Page-event capture with sensitive-header redaction, GraphQL op detection, WS events, redirects, bounded deques; explicitly designed as a SERA sensor | **Retain**, add action-correlation hooks (Phase 8) |
| `action_schema.py` | Strict allowlist + per-action arg typing + unknown-arg rejection + ref-existence + SSRF-on-navigate. Needs spec fixes (F3/F7), not redesign | **Retain with modifications** |
| `browser/manager.py`, `browser/context.py` | Correct lifecycle: one browser per process, isolated context per task, storage_state with 0600 perms, popup tracking, force-close safety nets | **Retain** — becomes the `LocalPlaywrightRuntime` internals (Phase 3) |
| `api/app.py` | Clean FastAPI surface incl. viewer endpoints + WS stream; graceful lifespan shutdown | **Retain**, extend with intelligence endpoints |
| `browser/viewer.py` | Real-view CDP streaming + human takeover — already consumed by SERA's chat UI (`assessments/[id]/page.tsx` WS events) | **Retain as-is** |
| `challenge/detector.py` | Comprehensive challenge taxonomy (Turnstile, interstitials, hCaptcha, reCAPTCHA, MFA, login challenges) + test-key awareness + HITL policy | **Retain as-is** |
| `session/manager.py` | Task lifecycle, isolation, cancellation propagation, viewer wiring | **Retain**, slim down as responsibilities move to the engine |
| `models.py` | Pydantic `BrowserState`/`AgentReport` — the SERA-facing contract | **Retain, extend additively** (locator specs, coverage, evidence, graph) |
| `task/llm_planner.py` (parts) | Cache-friendly state-view ordering (`llm_planner.py:544-650`), fence-tolerant parsing, forbidden-action guard, repetition-redirect machinery | **Retain the prompt/view builder; rebuild the escalation logic** |
| `task/memory.py` | Rolling step history + compaction design is sound; it just carries no payload (F5) | **Retain, feed it real results** |
| `task/history.py` | Masked model-facing views, replay concept | Retain after fixing `replay` (F7) or fold into evidence ledger |
| `task/observability.py`, `task/safety.py`, `task/extraction.py` | Wired, functional, SERA-visible | Retain |
| `browser/stealth.py`, `browser/storage.py`, `browser/screenshots.py`, `browser/tabs.py`, `browser/cdp_ax.py` | Functional, conditionally wired | Retain |

## 5. What must be replaced — REPLACE list

| Module | Why it must be replaced/rebuilt | Rebuild phase |
|---|---|---|
| Element identity + resolution (`driver._build_tree` refs, `inspector.find_by_ref`, `driver.click_ref/fill/...` resolution half) | F3: unstable refs, ambiguous resolution, 3 namespaces | Phase 4 |
| Time/retry policy (implicit in `driver` action methods + planner retries) | F1: 2×15s blind waits, no retry accounting | Phase 5 |
| Loop recovery (`task/loop.py` + escalation branches in both planners) | F2: no dom-history reset on vision path; recovery = more screenshots; whole-task `blocked` | Phases 10/11 |
| Deterministic planner (`task/planner.py`) | F6: 5-route cap, no coverage model, one escape | Phases 6/12 |
| Auth inference (`agent.py:412-414`, `state.mark_authenticated`, `_classify_page`) | F4: no evidence model | Phase 9 |
| Report workflows/auth synthesis (`task/report.py` parts) | F4/F6: heuristic labels presented as findings | Phases 7/9/15 |
| Vision escalation policy (`llm_planner._try_vision_escalation` + fallthrough) | F2: provider failure as strategy; no per-state disable | Phase 11 |
| The agent loop's observe/act coupling (`agent.py:277-430`) | F5: no result channel; double observe per step; planner owns progress | Phase 2 |

## 6. Public API compatibility contract (MUST HOLD during rebuild)

SERA (read-only, `backend/app/core/sera_browser_engine.py:112-124` and `:1894-2116`) imports today:

```
from sera_browser import (BrowserManager, ChallengeDetector, ContextManager, Inspector,
                          NetworkRecorder, PlaywrightDriver, SessionManager, Settings,
                          TaskCreate, ElementResolver)
from sera_browser.cancel import CancellationManager
```

Additionally SERA depends on behavior, not just symbols:
- `SessionManager.create(TaskCreate(...))` → `SessionRecord` with `.task`, `.state`, `.report`, `.status`, `.viewer` (SERA polls it).
- `BrowserViewer` streaming + takeover endpoints (`/tasks/{id}/stream`, `/human_takeover`, `/resume`) — SERA's chat UI renders these.
- `AgentReport` keys are copied verbatim into `understanding/browser_analysis.json` (`_persist_report`, SERA `sera_browser_engine.py:1641`) — **additive changes only**; renaming/removing keys breaks SERA's persistence.
- `NetworkRecorder.responses` entries are drained by SERA (`_drain_recorder`, `sera_browser_engine.py:831-897`) expecting fields `{url, method, status, request_headers, post_data, response_headers, graphql, graphql_operation}` and feeds them into `HttpRequest` rows + `AttackSurfaceGraph.enrich_url/enrich_graphql/enrich_console_signals`.
- The `analyze_site` structured-report keys are the fixed contract: `{status, target, title, technology, routes, apis, forms, authentication, workflows, network, challenges, observations, summary, steps, duration_ms}`.

**Rebuild rule:** internal module paths may change freely; the imports above must keep resolving (re-export shims are acceptable and expected); new capabilities are exposed additively (e.g. `BrowserIntelligenceEngine`, `BrowserRun` package, `/tasks/{id}/intelligence`).

## 7. Dependency graph (audited, as wired)

```
SessionManager ──► BrowserAgent ──► [planner selection: TaskPlanner | LLMTaskPlanner | PAV(actor=LLMTaskPlanner)]
      │                 │                        │
      ▼                 ▼                        ▼
BrowserManager   ActionExecutor ◄── validate_action(action_schema) ◄── parse_llm_response
      │           │    ▲ ▲                                    ▲
      ▼           │    │ └── LoopRecoveryManager ◄─────────────┘ (suggest_recovery)
ContextManager   │    └── Inspector.find_by_ref(state.interactive_elements)
      │          ▼
      ▼      PlaywrightDriver ──► click_ref/fill/... (role+name `.first` OR selector hint)
   Page ──► Inspector.observe ──► driver.snapshot (DOM walk; CDP only if snapshot_method∈{cdp,auto})
      │            │
      │            ├──► state.update_from_snapshot (replaces interactive_elements; merges forms/links/routes)
      │            └──► ChallengeDetector.detect
      ├──► NetworkRecorder (page events → deques → merge_network → state)
      ├──► BrowserViewer (CDP port, WS streaming, takeover)
      └──► Observability / MemoryManager / HistoryManager / SafetyGuard (wired via private attrs, agent.py:213-215)

DEAD (no incoming edges from the live path):
  refmap.py · resolver.py · som.py · trimmer.py · browser_use_fallback.py · eval/harness.py · pav.py(off)
```

Notable smells beyond the failures already listed:
- **Private-attribute injection** (`agent.py:213-215`): memory/safety/tabs/history are wired onto the executor by assigning `_history_manager`, `_soft_restrict`, `_storage_manager` post-construction — invisible coupling that the rebuild must replace with constructor dependencies.
- `state.py` merge helpers grow unbounded lists (`links`, `forms` merge across the whole run, `models.py:203-205`) — fine for reports, wrong as a working set; the coverage engine must own bounded discovery state.
- `merge_network` swaps deque *references* into state (`state.py:80-82`) — the "snapshot" is a live view; anything serializing state mid-run races the recorder.

---

## 8. Rebuild map — file-by-file (Phases 2–19)

Target layout (new tree `sera_browser/` — package name unchanged):

```
src/sera_browser/
├── __init__.py                  # MODIFY: keep every existing export; add new engine symbols
├── config.py                    # MODIFY: backend selector, budgets, vision policy
├── security.py                  # RETAIN
├── cancel.py                    # RETAIN
├── models.py                    # MODIFY: additive (locator specs, coverage, evidence, graph, auth states)
├── logging.py                   # RETAIN
├── runtime/                     # CREATE (Phase 3)
│   ├── base.py                  #   BrowserRuntime protocol: create_session/connect/new_page/navigate/
│   │                            #   screenshot/cookies/storage/network/close
│   ├── local.py                 #   LocalPlaywrightRuntime (absorbs manager.py + context.py + driver primitives)
│   └── kernel.py                #   KernelRuntime (connect to a remote Chromium/Kernel over CDP/WS)
├── observe/                     # CREATE (Phases 2 + 4)
│   ├── engine.py                #   ObservationEngine: snapshot → registry → state delta
│   ├── element_registry.py      #   ElementRegistry: el_017 refs, versioned, stale detection, locators
│   ├── a11y.py                  #   ARIA snapshot extraction (absorb driver._extract_dom_tree + cdp_ax.py;
│   │                            #   trimmer.py logic finally wired in here)
│   └── extract.py               #   structured extraction with REAL result payloads (fixes F5)
├── coverage/                    # CREATE (Phase 6)
│   └── engine.py                #   discovered/visited sets for routes·elements·forms·workflows·endpoints,
│                                #   failed_actions + blocked_targets ledgers, next-target queue
├── graph/                       # CREATE (Phases 7 + 8)
│   ├── application.py           #   ApplicationGraph: routes ↔ workflows ↔ forms ↔ endpoints
│   └── correlation.py           #   action → route change → network request → endpoint correlation
├── authengine/                  # CREATE (Phase 9)
│   └── state_machine.py         #   ANONYMOUS→LOGIN_ATTEMPT→AUTHENTICATED→…, evidence-driven transitions
├── engine/                      # CREATE (Phases 2 + 10)
│   ├── mission.py               #   BrowserIntelligenceEngine: observe→plan→execute→verify→update graph loop
│   └── loop_engine.py           #   LoopEngine: multi-signal detection + invalidate/mark-blocked/retarget
├── planning/                    # CREATE (Phases 2 + 12)
│   ├── deterministic.py         #   systematic explorer (links/buttons/forms/menus/routes; owns completeness
│   │                            #   when LLM is absent) — replaces task/planner.py
│   └── llm.py                   #   LLM prioritizer: proposes goals/targets; NEVER executes directly
├── vision/                      # CREATE (Phase 11)
│   └── policy.py                #   VisionPolicy: DOM-first; budget 2 attempts per state; disable on failure
├── evidence/                    # CREATE (Phase 14)
│   └── ledger.py                #   per-step structured evidence (action, result, network events, screenshot)
├── intelligence/                # CREATE (Phase 15)
│   └── package.py               #   BrowserRun builder: application/routes/workflows/forms/endpoints/
│                                #   authStates/networkEvents/storageMetadata/evidence/coverage
├── task/                        # TRANSITIONAL SHIMS
│   ├── agent.py                 # MODIFY → thin adapter: BrowserAgent(TaskConfig) delegates to engine.mission
│   ├── executor.py              # MODIFY → executes registry locators with budgets; keep class name/signature
│   ├── llm_planner.py           # MODIFY → adapter over planning/llm.py (keep LLMTaskPlanner symbol)
│   ├── action_schema.py         # MODIFY: el_#### refs; remove "download" or implement it; result fields
│   ├── llm_client.py            # RETAIN as-is
│   ├── loop.py                  # MODIFY → shim over engine/loop_engine.py (keep LoopRecoveryManager symbol)
│   ├── memory.py / history.py / safety.py / observability.py / extraction.py / report.py / pav.py / soft_restrict.py
│   │                            # see disposition table below
├── browser/                     # TRANSITIONAL SHIMS
│   ├── manager.py / context.py / driver.py   # MODIFY: become LocalPlaywrightRuntime internals; keep
│   │                                          # symbols importable (SERA contract)
│   ├── inspector.py             # MODIFY → shim over observe/engine.py (keep Inspector symbol)
│   ├── refmap.py / resolver.py  # MODIFY: re-implemented atop element_registry; symbols stay for SERA
│   ├── viewer.py / tabs.py / stealth.py / storage.py / screenshots.py / cdp_ax.py / challenge/  # RETAIN
│   ├── som.py                   # DELETE (dead; SoM folded into vision/policy if ever needed)
│   ├── trimmer.py               # DELETE (logic moves into observe/a11y.py)
│   └── browser_use_fallback.py  # DELETE (never invoked; reintroduce only behind a real integration test)
├── net/recorder.py              # MODIFY: + action-correlation hooks (Phase 8), otherwise retain
├── session/manager.py           # MODIFY: slimmer; same public surface
└── api/app.py                   # MODIFY: + GET /tasks/{id}/intelligence (BrowserRun package)
```

### Disposition table (exact)

| Path | Action | Notes |
|---|---|---|
| `task/agent.py` | **MODIFY (major)** | Keep `BrowserAgent`, `TaskConfig`, `TaskResult` names + `run(cfg, cancellation)` signature. Body becomes wiring of `engine/mission.py`. Delete the `attempt_safe_login → mark_authenticated` block (`agent.py:412-414`). Replace private-attr injection (`agent.py:213-215`) with constructor deps. |
| `task/executor.py` | **MODIFY (major)** | Keep `ActionExecutor`, `ActionResult`. Click/fill/select/... resolve via `ElementRegistry.locator(ref)` (strict locators), single `action_timeout_ms=8s` budget, `retry_count=1` after re-observe; return real payloads for extract/scrape/content/search_page into state (fixes F5). Implement or remove `download`. |
| `task/planner.py` | **REPLACE** | New `planning/deterministic.py` systematic explorer driven by `coverage/engine.py`; keep a `TaskPlanner` shim for compatibility. |
| `task/llm_planner.py` | **MODIFY (major)** | Keep `LLMTaskPlanner`, `LLMPlannerStats`, `SYSTEM_PROMPT` symbols. Fix the ref-format description (`:122-124`); route vision through `vision/policy.py`; remove the screenshot fallthrough (`:301-307`); always reset dom history on handled loops; element view from registry (no 40-element cliff — page the registry). |
| `task/loop.py` | **REPLACE** | `engine/loop_engine.py` detects same action/ref/URL/state/goal/failure/LLM-output/screenshot-hash; on trigger: invalidate target → record failure → mark blocked → refresh observation → choose different target. Keep `LoopRecoveryManager` shim. |
| `task/pav.py` | **DELETE** | Off-by-default pattern with no consumers in tests/CI; reintroduce later as a planning strategy if evidence demands it. |
| `task/action_schema.py` | **MODIFY** | Refs = `el_####`; per-action arg specs updated; remove `"download"` or implement; keep `parse_llm_response`/`validate_action` API. |
| `task/memory.py`, `task/history.py`, `task/safety.py`, `task/observability.py`, `task/extraction.py` | **MODIFY (light)** | Feed real results; fix `history.replay` placeholders (`history.py:157-163`). |
| `task/report.py` | **MODIFY (major)** | Builds the report from `intelligence/package.py` data; workflows = graph-derived, not labels; auth summary from the state machine. `AgentReport` stays additive-compatible. |
| `task/soft_restrict.py` | **RETAIN** | |
| `browser/driver.py` | **MODIFY (major)** | Split: interaction primitives move behind `runtime/local.py` with budget policy; snapshot/DOM-walk moves to `observe/a11y.py`. `PlaywrightDriver` symbol + method names stay importable (SERA contract). |
| `browser/refmap.py`, `browser/resolver.py` | **REWRITE (thin)** | Re-implemented on the registry; same exported symbols; now actually used. |
| `browser/som.py`, `browser/trimmer.py`, `browser/browser_use_fallback.py` | **DELETE** | Dead (F7). |
| `browser/inspector.py` | **MODIFY** | Shim over `observe/engine.py`; fix `submit_ref` fabrication (`:119-126`) and unify form-field/link refs through the registry (kill namespaces #2/#3, F3). |
| `browser/cdp_ax.py`, `stealth.py`, `storage.py`, `screenshots.py`, `tabs.py`, `viewer.py`, `challenge/detector.py` | **RETAIN** | |
| `browser/manager.py`, `browser/context.py` | **MODIFY (moderate)** | Become `runtime/local.py` internals; keep classes as public facades. |
| `net/recorder.py` | **MODIFY (light)** | Add per-step action correlation (Phase 8). |
| `state/state.py` | **MODIFY** | `mark_authenticated` becomes a transition request on the auth state machine; `_classify_page` demoted to a hint. |
| `models.py` | **MODIFY (additive)** | `InteractiveElement` + `locator` spec (role/name/text/test-id, per Playwright locator guidance); new `CoverageReport`, `EvidenceEntry`, `AuthStateRecord`, `BrowserRun` models. All existing fields unchanged. |
| `config.py` | **MODIFY** | Add `browser_backend: auto|kernel|local`, `action_timeout_ms=8_000`, `action_retry_count=1`, `vision_max_attempts_per_state=2`, `coverage_max_steps`, `llm_max_consecutive_fallbacks` **actually enforced**; default `snapshot_method="auto"`. |
| `api/app.py` | **MODIFY (light)** | Add `/tasks/{id}/intelligence`. |
| `session/manager.py` | **MODIFY (light)** | Same surface. |
| `eval/harness.py` | **MOVE** | → `evals/` (dev tool, not shipped). |
| `pyproject.toml` | **FIX** | Entry point → `sera_browser.__main__:main`; coverage source → `sera_browser`; package-data key fixed. |
| `.github/workflows/ci.yml` | **FIX** | `branches: [main]`; add juice-shop acceptance job (Phase 17) + failure-injection job (Phase 18) as required checks. |
| `docs/ARCHITECTURE.md` | **REWRITE** after Phase 2 lands | Current doc describes the unwired architecture (F7) — it must document the wired one. |

### New tests (CREATE)

```
tests/test_element_registry.py      # ref stability, stale rejection, ambiguity (two same-name buttons)
tests/test_action_budgets.py        # failed click ≤ ~10s end-to-end, retry-once, then blocked+continue
tests/test_loop_engine.py           # multi-signal loops; vision failure never becomes strategy
tests/test_coverage_engine.py       # discovered/visited/blocked ledgers; next-target selection
tests/test_auth_state_machine.py    # evidence-driven transitions; no "clicked login ⇒ authenticated"
tests/test_feedback_channel.py      # scrape/extract results visible to the next planner call
tests/test_failure_injection.py     # Phase 18 matrix (below) — REQUIRED in CI
tests/test_juice_shop_acceptance.py # Phase 17 scenario (below) — REQUIRED in CI, `juice_shop` marker
```

## 9. Phase 17 — Juice Shop acceptance test (the definition of "done")

Don't test "did Chrome launch". Test: **"Can SERA Browser autonomously understand Juice Shop?"**

```
Given Juice Shop on http://127.0.0.1:3000 (Docker), a task with NO LLM provider configured:
  1.  reaches / and observes                                       (t < 15s)
  2.  cookie-banner dismissal attempted ONCE; on failure → target marked blocked → mission continues
      (assert: no second click on the same banner ref; blocked_targets contains it)
  3.  discovers ≥ 12 internal routes                               (assert: coverage.discovered_routes ≥ 12)
  4.  visits ≥ 8 routes                                            (assert: coverage.visited_routes ≥ 8)
  5.  discovers the login form and its fields
  6.  captures /rest/* endpoints with parameters                    (assert: graph endpoints ≥ 10,
      incl. /rest/products/search)
  7.  correlates at least one UI action → route change → API call  (assert: graph has an edge of each type)
  8.  auth state machine reaches ANONYMOUS→(observed login form) without claiming AUTHENTICATED
  9.  NO step exceeds the action budget                            (assert: max step duration < 15s)
  10. NO identical (action, ref) executed more than 2×             (assert from evidence ledger)
  11. NO vision calls (no provider)                                (assert: vision_calls == 0)
  12. finishes within the task budget with status completed
  13. emits a BrowserRun package containing routes, forms, endpoints, workflows, auth, coverage, evidence
Then the same suite re-run with a MOCK LLM provider shows: LLM consulted, budgets respected,
      deterministic fallback after injected provider failures, same acceptance assertions pass.
```

## 10. Phase 18 — Failure-injection matrix (mandatory in CI)

| # | Injection | Expected outcome |
|---|---|---|
| 1 | stale ref (mutate DOM between plan and act) | action rejected → registry re-resolves or ref invalidated → ≤1 retry → continue |
| 2 | failed click (element never actionable) | ≤ ~10s, retry once, target marked blocked, mission continues |
| 3 | overlay intercepting clicks | detected as actionability failure, not a 30s hang; retarget |
| 4 | navigation timeout | partial state accepted, route marked slow/blocked, continue |
| 5 | LLM timeout | deterministic planner takes over; task completes |
| 6 | LLM 400/429/503 | error classified; after N consecutive (enforced `llm_max_consecutive_fallbacks`), LLM disabled for task; task completes |
| 7 | vision 400/503 | 2 attempts max → vision disabled for that state → DOM continues; **never loops** |
| 8 | unknown ref from LLM | validator rejects with actionable message; planner re-prompted once; then deterministic |
| 9 | browser disconnect mid-task | clean failure report, no leaked processes (force-kill verified) |
| 10 | Kernel disconnect (backend=kernel) | fallback to local runtime or clean termination, no hang |
| 11 | redirect outside allowlist | blocked by SSRF policy; recorded as evidence; continue |
| 12 | same action/ref/URL repetition ×3 | LoopEngine fires: invalidate, mark blocked, retarget — mission continues |

## 11. SERA-side changes (Phase 16 — for the second agent, read-only notes)

From the SERA repo exploration (no writes made or planned):

- **Dispatcher**: new task `run_browser_recon` registered in `register_all_async_impls` (`dispatcher.py:535-594`) + a `@celery_app.task` in `assessment_tasks.py`; per-run toggle in `Assessment.config` (schema at `db/schemas.py:151-196`, composer at `AssessmentComposer.tsx:178-191`).
- **Pipeline**: browser recon belongs early — either as part of `understanding` phase (its output `browser_summary.json`/`captured_requests.json` already load into later phases via `context_loader.py:130-138`) or as a `recon`-parallel stage in `_execute_deterministic_pipeline` (`assessment_tasks.py:370-392`).
- **Asset model**: SERA's `AttackSurfaceGraph` (`attack_surface.py:153+`) already offers the sinks: `enrich_url`, `enrich_forms`, `enrich_api_spec`, `enrich_graphql`, `enrich_identities`, `enrich_storage`, `enrich_console_signals` — the BrowserRun package should be drained there (extend `_drain_recorder`, `sera_browser_engine.py:831-897`, or add a one-time `sync_from_workspace` import of `browser_analysis.json`, `attack_surface.py:951-1027`).
- **Findings/evidence**: `create_finding` + `pocs/{finding_id}/` invariant; `Finding.screenshot_path` already exists — attach browser evidence paths there.
- **Specialists**: `specialist_router.py:393-476` — add routes keyed on browser-discovered signals (e.g. "endpoint requires auth ⇒ authz specialist").
- **UI**: assessment page tab array `page.tsx:437-443` — add `browser-recon` tab; new WS event types through `ws_manager.emit_event`.
- **Contract doc**: `docs/BROWSER-ENGINE.md` in SERA is the de-facto integration doc; update it when the intelligence package lands.

## 12. Recommended sequencing (risk-ordered)

1. **Hygiene P0 (half-day)**: fix `ci.yml` branches, `pyproject.toml` entry point + coverage source; make CI required. *Everything else depends on tests actually running.*
2. **Phase 4 + 5 (ElementRegistry + budgets)** — smallest diff, kills the two most visible failures (30s click, ref chaos) while keeping the current agent shape. Ship as `v3.2.0` once Juice Shop acceptance passes.
3. **Phase 3 (BrowserRuntime split)** — pure refactor behind the registry; enables `SBA_BROWSER_BACKEND=kernel` later without touching the engine.
4. **Phase 2 (engine/mission + feedback channel)** — the structural change; do it with the registry already stable.
5. **Phases 6/7/8 (coverage, graph, correlation)** — the value delivery for SERA; deterministic-first.
6. **Phases 9/10/11/12** — auth machine, loop engine, vision policy, deterministic completeness.
7. **Phases 13/14/15 + SERA-side 16** — security-aware collection, evidence ledger, BrowserRun package, SERA integration.
8. **Phases 17/18 run continuously** from step 2 onward as gates, not as a final phase.

## 13. Open questions for the repo owner

1. **Kernel** — no Kernel component exists in either repo today. Is the "Kernel" in the rebuild plan (a) a future SERA service hosting shared Chromium sessions over CDP, or (b) an external product? `runtime/kernel.py` is stubbed accordingly either way; the protocol is the deliverable, the transport can follow.
2. **Auth testing depth** — Phase 9's `LOGIN_ATTEMPT` transitions: should the agent ever submit credentials autonomously (test creds via `TaskCreate.auth`), or always pause for human takeover? Current code never logs in (planner phase 5 is a no-op inspect); the state machine needs a policy decision.
3. **`browser-use` fallback** — delete outright (recommended; it never runs) or keep as an extra behind a feature flag with a real integration test?
4. **PyPI** — the published 3.1.1 console script is broken (F8). Worth cutting a `v3.1.2` patch from `main` with just the packaging fixes while the rebuild proceeds on the branch.
