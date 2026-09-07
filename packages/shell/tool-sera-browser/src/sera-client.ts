/**
 * Thin HTTP + WebSocket client for the sera-browser sidecar service. Keeps the
 * tool body free of transport detail: `createTask`, `getTask`, `getResult`,
 * `cancelTask`, and `streamTask` map 1:1 onto the sera-browser REST/WS API.
 *
 * Node 24 ships a global `fetch` and a global `WebSocket`, so this client has
 * zero runtime dependencies — it talks to `http://127.0.0.1:8765` by default.
 *
 * @module @deepseek-ai/dsh-tool-sera-browser/sera-client
 */

/** sera-browser TaskCreate request body (mirrors the Python `TaskCreate` model). */
export interface SeraTaskCreate {
  target: string
  task: string
  browser_mode?: 'headless' | 'headful'
  timeout?: number
  local_lab?: boolean
  allowed_targets?: string[]
  auth?: { username?: string; password?: string }
}

/** sera-browser task record returned by POST /tasks. */
export interface SeraTaskCreated {
  task_id: string
  status: string
  target: string
}

/** sera-browser task status returned by GET /tasks/{id}. */
export interface SeraTaskStatus {
  task_id: string
  target: string
  task: string
  browser_mode: string
  status: string
  error: string | null
  started_at: number | null
  finished_at: number | null
  has_state: boolean
  has_report: boolean
}

/** Terminal statuses — once reached, polling stops. */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/** Is the given sera-browser status terminal (no further polling needed)? */
export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status)
}

/** sera-browser HTTP/WS client bound to one base URL. */
export class SeraClient {
  constructor(private readonly base: string) {}

  /** POST /tasks — create + start a browser agent task. */
  async createTask(req: SeraTaskCreate, signal?: AbortSignal): Promise<SeraTaskCreated> {
    const res = await fetch(`${this.base}/tasks`, {
      method: 'POST',
      ...(signal !== undefined ? { signal } : {}),
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(req),
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new Error(`sera-browser POST /tasks failed: HTTP ${res.status} ${body}`)
    }
    return await res.json() as SeraTaskCreated
  }

  /** GET /tasks/{id} — current task status + metadata. */
  async getTask(taskId: string, signal?: AbortSignal): Promise<SeraTaskStatus> {
    const res = await fetch(`${this.base}/tasks/${taskId}`, ...(signal !== undefined ? [{ signal }] : []))
    if (!res.ok) throw new Error(`sera-browser GET /tasks/${taskId} failed: HTTP ${res.status}`)
    return await res.json() as SeraTaskStatus
  }

  /** GET /tasks/{id}/result — final structured report (409 until ready). */
  async getResult(taskId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const res = await fetch(`${this.base}/tasks/${taskId}/result`, ...(signal !== undefined ? [{ signal }] : []))
    if (!res.ok) throw new Error(`sera-browser GET result failed: HTTP ${res.status}`)
    return await res.json() as Record<string, unknown>
  }

  /** POST /tasks/{id}/cancel — cancel a running task. */
  async cancelTask(taskId: string, signal?: AbortSignal): Promise<void> {
    const init: RequestInit = { method: 'POST' }
    if (signal !== undefined) init.signal = signal
    await fetch(`${this.base}/tasks/${taskId}/cancel`, init).catch(() => {})
  }

  /** GET /health — liveness probe (used at startup to confirm the sidecar is up). */
  async health(signal?: AbortSignal): Promise<{ status: string; browser_mode: string } | null> {
    try {
      const res = await fetch(`${this.base}/health`, ...(signal !== undefined ? [{ signal }] : []))
      if (!res.ok) return null
      return await res.json() as { status: string; browser_mode: string }
    } catch {
      return null
    }
  }

  /**
   * Open the sera-browser WebSocket screenshot stream and dispatch each frame
   * to `onEvent`. Resolves when the socket closes (task done, aborted, or the
   * `signal` aborts). Never rejects — callers poll GET /tasks/{id} for the
   * terminal status. Keeps the streaming path robust against socket errors.
   */
  streamTask(taskId: string, onEvent: (event: SeraStreamEvent) => void, signal?: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const wsUrl = `${this.base.replace(/^http/, 'ws')}/tasks/${taskId}/stream`
      let ws: WebSocket | undefined
      let closed = false
      const cleanup = () => {
        if (closed) return
        closed = true
        signal?.removeEventListener('abort', onAbort)
        if (ws !== undefined && ws.readyState !== WebSocket.CLOSED) {
          try { ws.close() } catch { /* ignore */ }
        }
        resolve()
      }
      const onAbort = () => cleanup()
      signal?.addEventListener('abort', onAbort)
      try {
        ws = new WebSocket(wsUrl)
      } catch {
        cleanup()
        return
      }
      ws.onmessage = (ev: MessageEvent) => {
        try {
          const data = typeof ev.data === 'string' ? JSON.parse(ev.data) : {}
          if (data.type === 'step' && data.data !== undefined) {
            onEvent({ kind: 'screenshot', ...data.data })
          } else if (data.type === 'takeover') {
            onEvent({ kind: 'takeover', ...(data.data ?? {}) })
          } else if (data.type === 'resume') {
            onEvent({ kind: 'resume', ...(data.data ?? {}) })
          }
        } catch { /* malformed frame — ignore, polling is the source of truth */ }
      }
      ws.onerror = () => { /* polling handles failure reporting; just wait for close */ }
      ws.onclose = () => cleanup()
    })
  }
}

/** Normalized stream frame forwarded to the client panel. */
export type SeraStreamEvent =
  | { kind: 'screenshot'; screenshot?: string; step?: number; action?: string; url?: string; title?: string }
  | { kind: 'takeover'; reason?: string }
  | { kind: 'resume' }
