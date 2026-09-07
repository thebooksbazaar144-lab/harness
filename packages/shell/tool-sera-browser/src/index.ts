/**
 * @deepseek-ai/dsh-tool-sera-browser — the cloud browser tool. Backs the
 * model-facing `sera_browser_browse` tool with the sera-browser automation
 * sidecar (a Playwright-first autonomous browser agent, see
 * `services/sera-browser/`). The agent gives it a target URL + an objective;
 * sera-browser drives a real browser to accomplish it while every step's
 * screenshot streams live to the user's Browser side panel.
 *
 * Headless mode (default) runs an invisible browser whose screenshots are the
 * user's eyes. Headful mode (`browser_mode: "headful"`) launches a visible
 * browser window with CDP remote debugging where a display is available — the
 * same screenshot stream still feeds the panel, and the user can also watch or
 * take over the local window.
 *
 * The tool itself only orchestrates: it POSTs a task, opens the screenshot
 * WebSocket, polls the task status, and returns a readable summary. sera-browser
 * owns all browser automation and is NOT modified by this integration.
 *
 * @module @deepseek-ai/dsh-tool-sera-browser
 */
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { GenericCallView, ToolResultView } from '@deepseek-ai/dsh-tools'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { SeraClient } from './sera-client.ts'
import type { SeraTaskCreate } from './sera-client.ts'
// Type-only: loads the sera-browser forwarded-event declarations (the Events
// interface augmentation + TypertRemoteEventSelection) from the remotes package,
// so ctx.emit('sera-browser/screenshot' | '/status', ...) typechecks.
import type {} from '@deepseek-ai/dsh-api-remotes'

export const name = 'tool-sera-browser'

/** This plugin waits for the tool registry and the system-prompt service. */
export const inject = ['tools', 'systemPrompt']

export interface Config {
  /** Base URL of the sera-browser sidecar (default http://127.0.0.1:8765). */
  baseUrl: string
  /** Default task timeout in seconds (the agent may override per call). */
  defaultTimeout: number
}

export const Config: z<Config> = z.object({
  baseUrl: z.string().default('http://127.0.0.1:8765'),
  defaultTimeout: z.number().default(180),
})

const BROWSER_MODES = ['headless', 'headful'] as const

const BROWSE_DESCRIPTION = [
  'Drive a real cloud browser (sera-browser, Playwright-backed) to accomplish a browsing objective.',
  'Use this whenever browser access is required: visiting a web page, analyzing a web app,',
  'filling a form, testing a flow, scraping, or any task that needs a real browser rendering.',
  'Every step streams live to the user\'s Browser side panel — they watch it happen.',
  'Pass browser_mode "headful" to launch a visible browser window (where a display exists);',
  '"headless" (default) runs invisibly and the screenshots ARE the user\'s view.',
  'Returns a structured summary: title, technologies, routes discovered, API endpoints,',
  'auth state, and the agent\'s own observations.',
].join(' ')

/** Format the sera-browser AgentReport into readable text for the model. */
function formatReport(report: Record<string, unknown>): string {
  const lines: string[] = []
  const push = (label: string, value: unknown): void => {
    if (value === undefined || value === null || value === '') return
    if (Array.isArray(value) && value.length === 0) return
    lines.push(`${label}: ${formatValue(value)}`)
  }
  const formatValue = (v: unknown): string => {
    if (Array.isArray(v)) return (v as unknown[]).map(formatValue).join(', ')
    if (typeof v === 'object' && v !== null) {
      const obj = v as Record<string, unknown>
      return Object.entries(obj).map(([k, val]) => `${k}=${val === undefined || val === null ? 'n/a' : String(val)}`).join('; ')
    }
    return String(v)
  }
  push('status', report.status)
  push('target', report.target)
  push('title', report.title)
  push('technology', report.technology)
  push('routes', report.routes)
  push('apis', report.apis)
  if (report.auth_states !== undefined && typeof report.auth_states === 'object') {
    const auth = report.auth_states as Record<string, unknown>
    push('auth_state', auth.state)
  }
  push('summary', report.summary)
  push('observations', report.observations)
  push('steps', report.steps)
  push('duration_ms', report.duration_ms)
  if (report.error !== undefined && report.error !== null && report.error !== '') {
    push('error', report.error)
  }
  return lines.length === 0 ? '(sera-browser returned an empty report)' : lines.join('\n')
}

/** Poll the task status until terminal, the signal aborts, or the deadline passes. */
async function pollUntilTerminal(
  client: SeraClient,
  taskId: string,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<string> {
  const deadline = Date.now() + timeoutMs + 30_000
  while (Date.now() < deadline) {
    if (signal.aborted) return 'cancelled'
    await new Promise(resolve => setTimeout(resolve, 2000))
    try {
      const status = await client.getTask(taskId, signal)
      if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
        return status.status
      }
    } catch (error) {
      if (signal.aborted) return 'cancelled'
      // Transient fetch failure — keep polling, sera-browser may be briefly busy.
    }
  }
  return 'timeout'
}

export function apply(ctx: Context, config: Config = { baseUrl: 'http://127.0.0.1:8765', defaultTimeout: 180 }): void {
  const client = new SeraClient(config.baseUrl)

  ctx.systemPrompt.section({
    name: 'tool:sera_browser',
    order: ctx.systemPrompt.getSectionOrder('TOOL_SERA_BROWSER'),
    text: [
      '## sera_browser_browse tool',
      'A cloud browser is available. Use `sera_browser_browse` to drive a real browser to visit URLs,',
      'analyze web apps, fill forms, test flows, and scrape. You receive a target URL and a natural-language',
      'objective; sera-browser autonomously drives the browser to accomplish it while the user watches live',
      'in their Browser side panel.',
      '',
      'When to use it: any time browser access is required (the user asks to visit a site, analyze a web app,',
      'test a login flow, etc.). Prefer it over `bash`+curl when JavaScript rendering, forms, or real browser',
      'behavior matters.',
      '',
      'Parameters: `target` (URL), `objective` (what to do), `browser_mode` (headless|headful, default headless),',
      '`timeout` (seconds, default 180), `local_lab` (allow localhost/private targets), `allowed_targets`',
      '(explicit host:port allowlist).',
    ].join('\n'),
  })

  ctx.tools.register(defineTool({
    name: 'sera_browser_browse',
    description: BROWSE_DESCRIPTION,
    parameters: {
      target: {
        type: 'string',
        required: true,
        description: 'The URL to navigate the browser to (e.g. "https://example.com").',
      },
      objective: {
        type: 'string',
        required: true,
        description: 'The natural-language objective for the browser agent to accomplish at the target.',
      },
      browser_mode: {
        type: 'string',
        enum: BROWSER_MODES,
        description: '"headless" (default, invisible browser — screenshots stream to the panel) or "headful" (visible browser window with CDP, where a display exists).',
      },
      timeout: {
        type: 'number',
        description: 'Task timeout in seconds (default 180). The browser agent stops if it cannot finish in time.',
      },
      local_lab: {
        type: 'boolean',
        description: 'Allow localhost / private-range targets (SSRF protection is on by default). Set true only for owned test sites.',
      },
      allowed_targets: {
        type: 'array',
        items: { type: 'string' },
        description: 'Explicit host[:port] allowlist when local_lab is true (e.g. ["localhost:3000", "127.0.0.1:3000"]).',
      },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          status: { type: 'string', required: true },
          taskId: { type: 'string', required: true },
          target: { type: 'string', required: true },
          summary: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary ?? '' }],
    },
    // Browser tasks can run minutes; the sera-browser task timeout + exec.signal
    // handle cancellation. This is the framework's fallback ceiling.
    timeoutMs: 600000,
    isConcurrencySafe: () => false,
    async execute(args: {
      target: string
      objective: string
      browser_mode?: 'headless' | 'headful'
      timeout?: number
      local_lab?: boolean
      allowed_targets?: string[]
    }, exec) {
      const sessionId: SessionId | undefined = exec.agent?.id
      const callId = exec.callId
      const taskTimeout = args.timeout ?? config.defaultTimeout

      // Create the task on the sera-browser sidecar.
      const createReq: SeraTaskCreate = {
        target: args.target,
        task: args.objective,
        browser_mode: args.browser_mode ?? 'headless',
        timeout: taskTimeout,
        ...(args.local_lab === true ? { local_lab: true } : {}),
        ...(args.allowed_targets !== undefined ? { allowed_targets: args.allowed_targets } : {}),
      }
      const created = await client.createTask(createReq, exec.signal)

      // Announce the task is running so the panel can show a placeholder.
      if (sessionId !== undefined) {
        ctx.emit('sera-browser/status', { sessionId, taskId: created.task_id, callId, status: 'running' })
      }

      // Stream screenshots in the background while polling for completion.
      let seq = 0
      const streamPromise = client.streamTask(
        created.task_id,
        (event) => {
          if (sessionId === undefined) return
          if (event.kind === 'screenshot' && event.screenshot !== undefined) {
            seq += 1
            ctx.emit('sera-browser/screenshot', {
              sessionId,
              taskId: created.task_id,
              callId,
              seq,
              dataUrl: event.screenshot,
              ...(event.url !== undefined ? { url: event.url } : {}),
              ...(event.title !== undefined ? { title: event.title } : {}),
              ...(event.step !== undefined ? { step: event.step } : {}),
              ...(event.action !== undefined ? { action: event.action } : {}),
            })
          }
        },
        exec.signal,
      )

      // Poll for terminal status. If aborted, cancel the sera-browser task too.
      let finalStatus: string
      try {
        finalStatus = await pollUntilTerminal(client, created.task_id, exec.signal, taskTimeout * 1000)
      } catch (error) {
        await client.cancelTask(created.task_id)
        finalStatus = 'failed'
      }
      if (exec.signal.aborted) {
        await client.cancelTask(created.task_id)
      }
      // Ensure the stream socket is closed before returning.
      await streamPromise

      // Fetch the final report (if completed) and announce terminal status.
      let summary = `sera-browser task ${created.task_id} ended with status: ${finalStatus}`
      if (finalStatus === 'completed') {
        try {
          const report = await client.getResult(created.task_id, exec.signal)
          summary = formatReport(report)
        } catch {
          summary = `sera-browser task ${created.task_id} completed but the report was unavailable.`
        }
      } else if (finalStatus === 'failed') {
        try {
          const status = await client.getTask(created.task_id)
          if (status.error !== null) summary = `sera-browser task failed: ${status.error}`
        } catch { /* keep default */ }
      }
      if (sessionId !== undefined) {
        ctx.emit('sera-browser/status', { sessionId, taskId: created.task_id, callId, status: finalStatus, detail: summary })
      }

      return {
        ok: finalStatus === 'completed',
        status: finalStatus,
        taskId: created.task_id,
        target: created.target,
        summary,
      }
    },
    presentCall: (args): GenericCallView => ({
      card: 'generic',
      kind: 'fetch',
      title: args.target,
      rawInput: args.objective,
    }),
    presentResult: (_args, result): ToolResultView | undefined => {
      if (result.isError) return undefined
      return { card: 'generic' }
    },
  }))
}
