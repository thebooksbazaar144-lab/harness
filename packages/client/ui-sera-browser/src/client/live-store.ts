/**
 * Per-session live browser state. The host forwards `sera-browser/screenshot`
 * and `sera-browser/status` events (emit-mode); this store holds the latest
 * frame per session and exposes a `useSyncExternalStore`-compatible surface so
 * the React panel re-renders only when its session's frame changes.
 *
 * One store per session, cached in a WeakMap keyed by the session binding so it
 * is garbage-collected when the session leaves the renderer.
 */

/** The live browser view for one session. */
export interface BrowserLiveState {
  /** Latest screenshot as a `data:image/…;base64,…` URL (undefined until first frame). */
  screenshot?: string
  /** sera-browser task id (undefined when no task has run). */
  taskId?: string
  /** The model tool call id that owns the current/last task. */
  callId?: string
  /** `running` | `completed` | `failed` | `cancelled` (undefined before any task). */
  status?: string
  /** 1-based agent step number from the last frame. */
  step?: number
  /** The action sera-browser just took (navigate/click/type/…). */
  action?: string
  /** Current page URL from the last frame. */
  url?: string
  /** Current page title from the last frame. */
  title?: string
  /** Detail string (error message or summary). */
  detail?: string
  /** Wall-clock ms of the last update (for "stale" detection in the UI). */
  updatedAt: number
}

export interface BrowserLiveStore {
  getSnapshot: () => BrowserLiveState
  subscribe: (listener: () => void) => () => void
  /** Apply one screenshot frame. */
  setScreenshot(frame: Partial<Omit<BrowserLiveState, 'updatedAt'>> & { screenshot: string }): void
  /** Apply one status change. */
  setStatus(status: string, detail?: string, taskId?: string, callId?: string): void
}

const EMPTY: BrowserLiveState = { updatedAt: 0 }

/** Create a fresh store bound to one session. */
export function createBrowserLiveStore(): BrowserLiveStore {
  let state: BrowserLiveState = EMPTY
  const listeners = new Set<() => void>()
  const emit = (): void => { for (const listener of listeners) listener() }
  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    setScreenshot: (frame) => {
      const next: BrowserLiveState = {
        screenshot: frame.screenshot,
        status: 'running',
        updatedAt: Date.now(),
      }
      if (frame.taskId !== undefined) next.taskId = frame.taskId
      if (frame.callId !== undefined) next.callId = frame.callId
      if (frame.step !== undefined) next.step = frame.step
      if (frame.action !== undefined) next.action = frame.action
      if (frame.url !== undefined) next.url = frame.url
      if (frame.title !== undefined) next.title = frame.title
      if (state.detail !== undefined) next.detail = state.detail
      state = next
      emit()
    },
    setStatus: (status, detail, taskId, callId) => {
      const next: BrowserLiveState = { ...state, status, updatedAt: Date.now() }
      if (detail !== undefined) next.detail = detail
      if (taskId !== undefined) next.taskId = taskId
      if (callId !== undefined) next.callId = callId
      state = next
      emit()
    },
  }
}
