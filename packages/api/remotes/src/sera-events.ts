/**
 * sera-browser forwarded event vocabulary. The remotes package owns both the
 * forwarded-event allowlist (API_REMOTE_FORWARDED_EVENTS) and these type
 * declarations, because the forwarder is the natural home for forwarded-event
 * types — it avoids cross-project augmentation cycles in the composite build.
 *
 * The host tool (`@deepseek-ai/dsh-tool-sera-browser`) emits these events while
 * a browser task streams; the client panel (`ui-sera-browser`) subscribes via
 * `ctx.remote.$on`.
 *
 * @module @deepseek-ai/dsh-api-remotes/sera-events
 */
import type { SessionId } from '@deepseek-ai/dsh-session/types'

/** One screenshot frame pushed from a running sera-browser task. */
export interface SeraScreenshotEvent {
  readonly sessionId: SessionId
  readonly taskId: string
  readonly callId: string
  readonly seq: number
  readonly dataUrl: string
  readonly url?: string
  readonly title?: string
  readonly step?: number
  readonly action?: string
}

/** Task lifecycle status pushed to the panel. */
export interface SeraStatusEvent {
  readonly sessionId: SessionId
  readonly taskId: string
  readonly callId: string
  readonly status: string
  readonly detail?: string
}

declare module '@deepseek-ai/cordis' {
  interface Events {
    /** One live screenshot frame from a running sera-browser task. @mode emit */
    'sera-browser/screenshot'(payload: SeraScreenshotEvent): void
    /** sera-browser task lifecycle status change. @mode emit */
    'sera-browser/status'(payload: SeraStatusEvent): void
  }
}

declare module '@deepseek-ai/dsh-typert-protocol' {
  interface TypertRemoteEventSelection
    extends Record<'sera-browser/screenshot' | 'sera-browser/status', true> {}
}

export {}
