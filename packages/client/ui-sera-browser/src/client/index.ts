/**
 * sera-browser client panel plugin — registers the "Browser" tab in the
 * conversation view. While the agent drives the cloud browser (via the
 * `sera_browser_browse` host tool), every screenshot frame forwarded from the
 * host lands here and renders live beside the chat.
 *
 * @module @deepseek-ai/dsh-client-ui-sera-browser/client
 */
import type { Context } from '@deepseek-ai/cordis'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import type { SessionBinding } from '@deepseek-ai/dsh-api-session-controller/client'
// Type-only: pulls the locale, slots, renderer, session, and remote service
// merges into this program so the register calls typecheck.
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-session/client'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import { en, NS, zh } from './locales.ts'
import {
  createBrowserLiveStore, type BrowserLiveStore,
} from './live-store.ts'
import { SeraBrowserPanel, type SeraBrowserInjected } from './SeraBrowserPanel.tsx'

export type { SeraBrowserInjected } from './SeraBrowserPanel.tsx'

/** Required services: the slot registry, session bindings, locale, and remote forwarded events. */
export const inject = ['slots', 'sessions', 'uiSession', 'locale', 'remote']

/**
 * Client plugin body: register the Browser view tab and wire the forwarded
 * screenshot/status events into per-session live stores the panel reads.
 * @param ctx - client root context.
 */
export function apply(ctx: Context): void {
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'ui-sera-browser: dictionaries')
  const t = ctx.locale.bind(NS)

  // One live store per session binding; WeakMap so it dies when the binding does.
  const stores = new WeakMap<SessionBinding, BrowserLiveStore>()
  const storeFor = (binding: SessionBinding): BrowserLiveStore => {
    let store = stores.get(binding)
    if (store === undefined) {
      store = createBrowserLiveStore()
      stores.set(binding, store)
    }
    return store
  }

  // Forwarded-event listeners: write host frames into the matching session store.
  ctx.remote.$on('sera-browser/screenshot', (payload) => {
    const binding = ctx.sessions.binding(payload.sessionId)
    if (binding === undefined) return
    storeFor(binding).setScreenshot({
      screenshot: payload.dataUrl,
      taskId: payload.taskId,
      callId: payload.callId,
      ...(payload.step !== undefined ? { step: payload.step } : {}),
      ...(payload.action !== undefined ? { action: payload.action } : {}),
      ...(payload.url !== undefined ? { url: payload.url } : {}),
      ...(payload.title !== undefined ? { title: payload.title } : {}),
    })
  })
  ctx.remote.$on('sera-browser/status', (payload) => {
    const binding = ctx.sessions.binding(payload.sessionId)
    if (binding === undefined) return
    storeFor(binding).setStatus(payload.status, payload.detail, payload.taskId, payload.callId)
  })

  // Register the Browser tab in the conversation view slot.
  ctx.slots.inject('conversation.view', () => ctx.slots.register({
    name: 'conversation.view',
    id: 'sera-browser',
    order: 30,
    locale: NS,
    label: () => t('view.browser'),
    inject: (sessionId: SessionId): SeraBrowserInjected => {
      const binding = ctx.sessions.binding(sessionId)
      if (binding === undefined) {
        throw new Error(`ui-sera-browser: session "${sessionId}" is unavailable`)
      }
      return {
        sessionId,
        live: storeFor(binding),
        t,
      }
    },
  }, SeraBrowserPanel))
}
