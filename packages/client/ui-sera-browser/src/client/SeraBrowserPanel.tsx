/**
 * The Browser side panel. Subscribes to the per-session live store (fed by
 * forwarded `sera-browser/screenshot` events) and renders the latest frame.
 * Shows the task status, current step/action/URL, and a placeholder when no
 * browser task is active.
 */
import { useSyncExternalStore } from 'react'
import type { BrowserLiveState, BrowserLiveStore } from './live-store.ts'
import type { en } from './locales.ts'
import type { SessionId } from '@deepseek-ai/dsh-session/types'

export interface SeraBrowserInjected {
  readonly sessionId: SessionId
  readonly live: BrowserLiveStore
  readonly t: (key: keyof typeof en) => string
}

const STATUS_BADGE: Record<string, string> = {
  running: 'sera-badge sera-badge-running',
  completed: 'sera-badge sera-badge-completed',
  failed: 'sera-badge sera-badge-failed',
  cancelled: 'sera-badge sera-badge-cancelled',
}

const STATUS_LABEL_KEY: Record<string, keyof typeof en> = {
  running: 'panel.running',
  completed: 'panel.completed',
  failed: 'panel.failed',
  cancelled: 'panel.cancelled',
}

export function SeraBrowserPanel(props: SeraBrowserInjected): JSX.Element {
  const state: BrowserLiveState = useSyncExternalStore(props.live.subscribe, props.live.getSnapshot)
  const t = props.t
  const hasScreenshot = state.screenshot !== undefined
  const status = state.status
  const badgeClass = status !== undefined ? (STATUS_BADGE[status] ?? 'sera-badge') : 'sera-badge'
  const labelKey = status !== undefined ? STATUS_LABEL_KEY[status] : undefined
  const statusLabel = labelKey !== undefined ? t(labelKey) : (status ?? '')

  return (
    <div className="sera-browser-panel">
      <div className="sera-browser-header">
        <span className="sera-browser-title">{t('view.browser')}</span>
        {status !== undefined && <span className={badgeClass}>{statusLabel}</span>}
      </div>

      <div className="sera-browser-viewport">
        {hasScreenshot
          ? (
            <img
              className="sera-browser-screenshot"
              src={state.screenshot}
              alt={state.title ?? state.url ?? 'browser screenshot'}
            />
          )
          : (
            <div className="sera-browser-placeholder">
              {status === 'running'
                ? t('panel.waiting')
                : t('panel.placeholder')}
            </div>
          )}
      </div>

      {(hasScreenshot || state.url !== undefined || state.step !== undefined) && (
        <div className="sera-browser-meta">
          {state.step !== undefined && (
            <div className="sera-meta-row">
              <span className="sera-meta-label">{t('panel.step')}</span>
              <span className="sera-meta-value">{state.step}</span>
              {state.action !== undefined && (
                <span className="sera-meta-action">· {state.action}</span>
              )}
            </div>
          )}
          {state.url !== undefined && state.url !== '' && (
            <div className="sera-meta-row">
              <span className="sera-meta-label">{t('panel.url')}</span>
              <span className="sera-meta-value sera-meta-url" title={state.url}>{state.url}</span>
            </div>
          )}
          {state.title !== undefined && state.title !== '' && (
            <div className="sera-meta-row">
              <span className="sera-meta-label">{t('panel.title')}</span>
              <span className="sera-meta-value">{state.title}</span>
            </div>
          )}
          {state.detail !== undefined && state.detail !== '' && status !== 'running' && (
            <div className="sera-meta-detail">{state.detail}</div>
          )}
        </div>
      )}

      <style>{`
        .sera-browser-panel {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--dsh-color-bg-elevated, #0d1117);
          color: var(--dsh-color-text, #e6edf3);
          font-family: var(--dsh-font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
          border-left: 1px solid var(--dsh-color-border, #30363d);
        }
        .sera-browser-header {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 10px 14px;
          border-bottom: 1px solid var(--dsh-color-border, #30363d);
          background: var(--dsh-color-bg-subtle, #161b22);
        }
        .sera-browser-title {
          font-size: 13px;
          font-weight: 600;
          letter-spacing: 0.02em;
        }
        .sera-badge {
          font-size: 11px;
          padding: 2px 8px;
          border-radius: 999px;
          font-weight: 500;
          text-transform: capitalize;
        }
        .sera-badge-running { background: rgba(46, 160, 67, 0.18); color: #3fb950; }
        .sera-badge-completed { background: rgba(46, 160, 67, 0.22); color: #3fb950; }
        .sera-badge-failed { background: rgba(248, 81, 73, 0.18); color: #f85149; }
        .sera-badge-cancelled { background: rgba(139, 148, 158, 0.18); color: #8b949e; }
        .sera-browser-viewport {
          flex: 1 1 auto;
          min-height: 0;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 12px;
          overflow: hidden;
          background: #000;
        }
        .sera-browser-screenshot {
          max-width: 100%;
          max-height: 100%;
          object-fit: contain;
          border-radius: 6px;
          box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
        }
        .sera-browser-placeholder {
          color: #8b949e;
          font-size: 13px;
          text-align: center;
          max-width: 320px;
          line-height: 1.5;
        }
        .sera-browser-meta {
          padding: 8px 14px 12px;
          border-top: 1px solid var(--dsh-color-border, #30363d);
          background: var(--dsh-color-bg-subtle, #161b22);
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .sera-meta-row {
          display: flex;
          align-items: baseline;
          gap: 6px;
          font-size: 12px;
        }
        .sera-meta-label {
          color: #8b949e;
          flex-shrink: 0;
          width: 44px;
        }
        .sera-meta-value {
          color: #e6edf3;
          word-break: break-all;
        }
        .sera-meta-url { font-family: var(--dsh-font-mono, ui-monospace, SFMono-Regular, monospace); font-size: 11px; }
        .sera-meta-action { color: #8b949e; }
        .sera-meta-detail {
          font-size: 11px;
          color: #8b949e;
          margin-top: 4px;
          line-height: 1.45;
          max-height: 96px;
          overflow-y: auto;
        }
      `}</style>
    </div>
  )
}
