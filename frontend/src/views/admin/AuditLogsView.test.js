import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import AuditLogsView from './AuditLogsView.vue'
import api from '../../utils/api'
vi.mock('../../utils/api', () => ({ default: { get: vi.fn() } }))
vi.mock('../../stores/auth', async () => {
  const { reactive } = await import('vue')
  return {
    useAuthStore: () =>
      reactive({ user: { id: 7 }, token: 'existing-session', isSuperAdmin: false }),
  }
})
vi.mock('vue-i18n', () => ({
  useI18n: () => ({ tm: () => ({}), locale: { value: 'en' } }),
}))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent, anchor) => {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove: (child) => {
    const list = child.parent?.children
    const index = list?.indexOf(child)
    if (index >= 0) list.splice(index, 1)
  },
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: () => {},
})

let app
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
afterEach(() => {
  app?.unmount()
  app = null
  vi.clearAllMocks()
})
it('defaults off, resets the page on toggle, and ignores old read-inclusive results', async () => {
  const pending = []
  api.get.mockImplementation(
    (path, config) =>
      new Promise((resolve) => {
        pending.push({ path, ...config, resolve })
      })
  )
  app = renderer.createApp(AuditLogsView)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.directive('loading', {})
  app.mount({ children: [] })
  const view = app._instance.setupState
  expect(pending[0].params).toEqual({ page: 1, limit: 50, include_read_events: false })
  pending[0].resolve({ data: { total: 0, items: [] } })
  await flush()
  view.page = 3
  view.filters.includeReadEvents = true
  const inclusive = view.fetchLogs()
  expect(pending[1].params.include_read_events).toBe(true)
  view.filters.includeReadEvents = false
  view.onFilterChange()
  expect(view.page).toBe(1)
  expect(pending[2].params).toEqual({ page: 1, limit: 50, include_read_events: false })
  expect(pending[1].signal.aborted).toBe(true)
  pending[1].resolve({ data: { total: 90, items: [{ action: 'task_notes.read' }] } })
  await inclusive
  expect(view.logs).toEqual([])
  expect(view.total).toBe(0)
  pending[2].resolve({ data: { total: 1, items: [{ action: 'bot.start' }] } })
  await flush()
  expect(view.logs).toEqual([{ action: 'bot.start' }])
  expect(view.total).toBe(1)
  view.filters.action = 'task_notes.denied'
  view.onFilterChange()
  expect(pending[3].params).toMatchObject({
    action: 'task_notes.denied',
    include_read_events: false,
  })
  pending[3].resolve({ data: { total: 1, items: [{ action: 'task_notes.denied' }] } })
  await flush()
  app.unmount()
  app = null
})

it('resets the credential tab on session changes and loss of the super administrator role', async () => {
  api.get.mockImplementation((path) =>
    Promise.resolve({ data: path === '/admin/bots' ? [] : { items: [], total: 0 } })
  )
  app = renderer.createApp(AuditLogsView)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.directive('loading', {})
  app.mount({ children: [] })
  const view = app._instance.setupState
  view.activeTab = 'credentials'
  await flush()
  expect(api.get.mock.calls.some(([path]) => path === '/admin/audit-credentials')).toBe(false)
  view.authStore.isSuperAdmin = true
  await flush()
  view.activeTab = 'credentials'
  await flush()
  expect(view.activeTab).toBe('credentials')
  view.authStore.token = 'replacement-session'
  await flush()
  expect(view.activeTab).toBe('logs')
  view.activeTab = 'credentials'
  view.authStore.isSuperAdmin = false
  await flush()
  expect(view.activeTab).toBe('logs')
  const count = api.get.mock.calls.length
  view.activeTab = 'credentials'
  await flush()
  expect(api.get.mock.calls).toHaveLength(count)
})
