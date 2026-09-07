/** Locale strings for the Browser side panel. */
export const NS = 'sera-browser'

/** Keys of the English dictionary — the namespace's key set. */
export type SeraBrowserKey = keyof typeof en

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** The cloud-browser side panel copy (tab label, status, meta rows). */
    'sera-browser': SeraBrowserKey
  }
}

export const en = {
  'view.browser': 'Browser',
  'panel.placeholder': 'No active browser task. Ask the agent to browse a site and it will appear here live.',
  'panel.running': 'Browser running',
  'panel.completed': 'Browser task completed',
  'panel.failed': 'Browser task failed',
  'panel.cancelled': 'Browser task cancelled',
  'panel.step': 'Step',
  'panel.action': 'Action',
  'panel.url': 'URL',
  'panel.title': 'Title',
  'panel.waiting': 'Waiting for the first screenshot…',
}

export const zh = {
  'view.browser': '浏览器',
  'panel.placeholder': '当前没有浏览器任务。让代理浏览某个网站，实时画面将显示在此处。',
  'panel.running': '浏览器运行中',
  'panel.completed': '浏览器任务已完成',
  'panel.failed': '浏览器任务失败',
  'panel.cancelled': '浏览器任务已取消',
  'panel.step': '步骤',
  'panel.action': '操作',
  'panel.url': '网址',
  'panel.title': '标题',
  'panel.waiting': '等待第一张截图…',
}
