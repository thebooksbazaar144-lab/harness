"""Local test site — a small FastAPI app.

Used by the test suite as the always-available target. Exposes:
- static + SPA-ish navigation
- forms (login + search + feedback)
- a /api/* and /rest/* surface
- a WebSocket echo endpoint
- a download endpoint
- a page that emits console errors + page errors
- a fake challenge page (Turnstile test sitekey)

Run with:
    python -m tests.fixtures.local_site.app --port 9999
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse


# Cloudflare Turnstile test sitekey (always passes) — documented for testing.
TURNSTILE_TEST_SITEKEY = "1x00000000000000000000AA"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SERA Local Test Site</title>
  <meta name="generator" content="sera-test-site">
  <script>window.__TEST_SITE__ = true; window.jQuery = {fn:function(){}};</script>
</head>
<body>
  <h1>SERA Local Test Site</h1>
  <nav>
    <a href="/">Home</a>
    <a href="/login">Login</a>
    <a href="/search">Search</a>
    <a href="/products">Products</a>
    <a href="/feedback">Feedback</a>
    <a href="/api/items">API</a>
    <a href="/ws">WebSocket echo</a>
    <a href="/challenge">Challenge</a>
    <a href="/errors">Errors</a>
    <a href="/download">Download</a>
  </nav>
  <main id="app"></main>
  <script>
    // tiny SPA: render based on hash
    const app = document.getElementById('app');
    function renderHome() {
      app.innerHTML = '<p>Welcome. Try the nav links.</p>';
    }
    renderHome();
    // Some apps lazy-load via fetch — do that here too.
    fetch('/rest/config').then(r => r.json()).then(c => {
      window.__CONFIG__ = c;
    }).catch(() => {});
  </script>
</body>
</html>
"""


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Login</title></head>
<body>
  <h1>Sign in</h1>
  <form action="/api/login" method="POST">
    <input type="text" name="username" placeholder="Username" required>
    <input type="password" name="password" placeholder="Password" required>
    <input type="hidden" name="csrf_token" value="abc123">
    <button type="submit">Sign in</button>
  </form>
  <p>Default credentials: guest / guest</p>
</body>
</html>
"""


SEARCH_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Search</title></head>
<body>
  <h1>Search</h1>
  <form action="/api/search" method="GET">
    <input type="search" name="q" placeholder="Search...">
    <button type="submit">Search</button>
  </form>
  <div id="results"></div>
  <script>
    // SPA-ish: render results client-side from query string
    const params = new URLSearchParams(location.search);
    if (params.get('q')) {
      document.getElementById('results').innerText = 'Searching for ' + params.get('q') + '...';
      fetch('/rest/items?q=' + encodeURIComponent(params.get('q')))
        .then(r => r.json())
        .then(items => {
          document.getElementById('results').innerText = JSON.stringify(items);
        })
        .catch(e => console.error('search failed', e));
    }
  </script>
</body>
</html>
"""


PRODUCTS_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Products</title></head>
<body>
  <h1>Products</h1>
  <ul>
    <li>Widget A — $10</li>
    <li>Widget B — $20</li>
    <li>Widget C — $30</li>
  </ul>
  <div id="more"></div>
  <script>
    fetch('/rest/items').then(r => r.json()).then(items => {
      document.getElementById('more').innerText = items.length + ' items loaded via API';
    }).catch(() => {});
  </script>
</body>
</html>
"""


FEEDBACK_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Feedback</title></head>
<body>
  <h1>Feedback</h1>
  <form action="/api/feedback" method="POST">
    <textarea name="message" placeholder="Your feedback..."></textarea>
    <select name="rating">
      <option value="1">1</option>
      <option value="2">2</option>
      <option value="3">3</option>
    </select>
    <label><input type="checkbox" name="anonymous"> Anonymous</label>
    <button type="submit">Submit</button>
  </form>
</body>
</html>
"""


CHALLENGE_HTML = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Verify you are human</title></head>
<body>
  <h1>Verify you are human</h1>
  <p>Please complete the challenge below.</p>
  <!-- Cloudflare Turnstile test sitekey (always passes) -->
  <div class="cf-turnstile" data-sitekey="{TURNSTILE_TEST_SITEKEY}"></div>
  <form action="/api/verify" method="POST">
    <button type="submit" disabled>Continue</button>
  </form>
</body>
</html>
"""


STUCK_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Stuck page</title></head>
<body>
  <h1>Stuck page</h1>
  <p>A visible button permanently covered by an overlay: it exists, it is
  visible, but it is NEVER clickable. This reproduces the v3 '30-second
  click' pathology for the action-budget regression tests.</p>
  <div style="position:relative;height:120px;">
    <button id="stuck-btn" type="button" style="position:absolute;top:0;left:0;">Stuck Button</button>
    <div id="overlay" style="position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:10;"></div>
  </div>
</body>
</html>
"""


ERRORS_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Errors page</title></head>
<body>
  <h1>Errors page</h1>
  <p>This page emits console errors and a page error.</p>
  <script>
    console.error('intentional console error');
    console.warn('intentional warning');
    // Trigger an uncaught error
    setTimeout(() => { undefinedVariable.notDefined(); }, 10);
  </script>
</body>
</html>
"""


WS_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>WebSocket echo</title></head>
<body>
  <h1>WebSocket echo</h1>
  <p>Opens a WebSocket to /ws/echo and sends a frame.</p>
  <pre id="log"></pre>
  <script>
    const log = document.getElementById('log');
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/echo');
    ws.onopen = () => { ws.send('hello'); log.innerText += 'sent hello\\n'; };
    ws.onmessage = (e) => { log.innerText += 'recv ' + e.data + '\\n'; };
    ws.onerror = (e) => { log.innerText += 'error\\n'; };
  </script>
</body>
</html>
"""


def create_app() -> FastAPI:
    app = FastAPI(title="SERA Local Test Site", docs_url="/docs")

    # --- HTML pages ---
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> str:
        return LOGIN_HTML

    @app.get("/search", response_class=HTMLResponse)
    async def search_page() -> str:
        return SEARCH_HTML

    @app.get("/products", response_class=HTMLResponse)
    async def products_page() -> str:
        return PRODUCTS_HTML

    @app.get("/feedback", response_class=HTMLResponse)
    async def feedback_page() -> str:
        return FEEDBACK_HTML

    @app.get("/challenge", response_class=HTMLResponse)
    async def challenge_page() -> str:
        return CHALLENGE_HTML

    @app.get("/errors", response_class=HTMLResponse)
    async def errors_page() -> str:
        return ERRORS_HTML

    @app.get("/ws", response_class=HTMLResponse)
    async def ws_page() -> str:
        return WS_HTML

    @app.get("/stuck", response_class=HTMLResponse)
    async def stuck_page() -> str:
        return STUCK_HTML

    # --- REST / API surface ---
    ITEMS = [
        {"id": 1, "name": "Widget A", "price": 10},
        {"id": 2, "name": "Widget B", "price": 20},
        {"id": 3, "name": "Widget C", "price": 30},
    ]

    @app.get("/rest/config")
    async def rest_config() -> dict:
        return {"theme": "light", "version": "0.1.0"}

    @app.get("/rest/items")
    async def rest_items(q: str | None = None) -> list:
        if q:
            return [i for i in ITEMS if q.lower() in i["name"].lower()]
        return ITEMS

    @app.get("/api/items")
    async def api_items() -> list:
        return ITEMS

    @app.post("/api/login")
    async def api_login(username: str = "", password: str = "") -> dict:
        if username == "guest" and password == "guest":
            return {"status": "ok", "token": "session-token-abc"}
        return {"status": "error", "message": "invalid credentials"}

    @app.get("/api/search")
    async def api_search(q: str = "") -> dict:
        return {"query": q, "results": [i for i in ITEMS if q.lower() in i["name"].lower()]}

    @app.post("/api/feedback")
    async def api_feedback(message: str = "", rating: str = "1", anonymous: bool = False) -> dict:
        return {"status": "ok", "received": {"message": message[:80], "rating": rating, "anonymous": anonymous}}

    @app.post("/api/verify")
    async def api_verify() -> dict:
        return {"status": "verified"}

    @app.get("/api/graphql")
    async def graphql_endpoint() -> dict:
        # Fake GraphQL introspection stub (real POST handler below).
        return {"data": None, "errors": [{"message": "Use POST for GraphQL"}]}

    @app.post("/api/graphql")
    async def graphql_post(request: dict) -> dict:
        # Minimal fake GraphQL: returns operationName in response.
        op = request.get("operationName") if isinstance(request, dict) else None
        return {"data": {"echo": True}, "extensions": {"operationName": op}}

    # --- Downloads ---
    @app.get("/download")
    async def download():
        data = b"hello world\nthis is a test file\n"
        headers = {"Content-Disposition": "attachment; filename=test.txt"}
        return StreamingResponse(
            iter([data]),
            media_type="text/plain",
            headers=headers,
        )

    # --- WebSocket ---
    @app.websocket("/ws/echo")
    async def ws_echo(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_text()
                await ws.send_text(f"echo: {msg}")
        except WebSocketDisconnect:
            return

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
