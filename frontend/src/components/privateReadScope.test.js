import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import AgentActivity from './AgentActivity.vue'
import AgentHistory from './AgentHistory.vue'
import BotAudit from './BotAudit.vue'
import ServiceTrafficPanel from './ServiceTrafficPanel.vue'
import api from '../utils/api'

const state = vi.hoisted(() => ({ auth: null, pending: [] }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => state.auth }))
vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: { value: 'en' } }) }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent) => {
    child.parent = parent
    parent.children.push(child)
  },
  remove: (child) => {
    const list = child.parent?.children
    if (list) list.splice(list.indexOf(child), 1)
  },
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: () => null,
  patchProp: () => {},
})
let app
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
function mount(component, initial = { botId: 1, includePrivate: true }) {
  const props = reactive(initial)
  app = renderer.createApp({ setup: () => () => h(component, props) })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.config.globalProperties.$tm = () => ({})
  app.directive('loading', {})
  app.mount({ children: [] })
  return { props, view: app._instance.subTree.component.setupState }
}
beforeEach(() => {
  vi.clearAllMocks()
  state.auth = reactive({ isSuperAdmin: true })
  state.pending = []
  api.get.mockImplementation(
    (path, options) => new Promise((resolve) => state.pending.push({ path, options, resolve }))
  )
  vi.stubGlobal('document', {
    hidden: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('uses public reads in Agent history and ignores stale requests after switching bots', async () => {
  const { props, view } = mount(AgentHistory, { botId: 1 })
  const old = state.pending[0]
  expect(old.options.params.include_private).toBe(false)
  props.botId = 2
  await nextTick()
  expect(old.options.signal.aborted).toBe(true)
  expect(state.pending[1].path).toBe('/bots/2/agent-history')
  expect(state.pending[1].options.params.include_private).toBe(false)
  old.resolve({ data: { items: [{ public_task: 'old bot' }] } })
  await flush()
  expect(view.items).toEqual([])
})

it('pages through ten Agent operations and resets the cursor when filtering by version', async () => {
  const { view } = mount(AgentHistory, { botId: 1 })
  expect(state.pending[0].options.params).toMatchObject({ limit: 10, cursor: undefined })
  state.pending[0].resolve({ data: { items: [], next_cursor: 'next-page' } })
  await flush()
  view.next()
  expect(state.pending[1].options.params.cursor).toBe('next-page')
  view.version = 'v2'
  view.reset()
  expect(view.page).toBe(0)
  expect(state.pending[1].options.signal.aborted).toBe(true)
  expect(state.pending[2].options.params).toMatchObject({
    version: 'v2',
    limit: 10,
    cursor: undefined,
  })
})

it('combines the version and authorization filters for the full Agent token inventory', () => {
  const { view } = mount(AgentActivity, {})
  view.page = 3
  view.version = 'v1'
  view.status = 'history'
  view.changeFilter()
  expect(state.pending[1].path).toBe('/admin/agent-activity')
  expect(state.pending[1].options.params).toMatchObject({
    include_legacy: true,
    version: 'v1',
    status: 'history',
    page: 1,
  })
})

it('clears private authorization audit rows immediately after closing consent', async () => {
  const { view, props } = mount(BotAudit)
  expect(state.pending[0].options.params.include_private).toBe(true)
  view.items = [{ purpose: 'fictional-private' }]
  props.includePrivate = false
  await nextTick()
  expect(view.items).toEqual([])
  expect(state.pending.at(-1).options.params.include_private).toBe(false)
  state.pending[0].resolve({ data: { items: [{ purpose: 'fictional-late-private' }], total: 1 } })
  await flush()
  expect(view.items).toEqual([])
})

it('keeps monitoring public when filtering bots or receiving a detailed-note prop', async () => {
  vi.useFakeTimers()
  const { view } = mount(ServiceTrafficPanel, { botId: null, includePrivate: true })
  expect(state.pending[0].options.params.include_private).toBe(false)
  state.pending[0].resolve({ data: null })
  await flush()
  const count = state.pending.length
  await vi.advanceTimersByTimeAsync(16000)
  expect(state.pending.length).toBeGreaterThan(count)
  expect(state.pending.at(-1).options.params.include_private).toBe(false)
  view.selectedBot = 2
  expect(view.snapshot).toBe(null)
  expect(state.pending.at(-1).options.params).toMatchObject({ bot_id: 2, include_private: false })
})

it('discards a late monitoring snapshot after switching the bot filter', async () => {
  const { view } = mount(ServiceTrafficPanel, { botId: null })
  const oldRead = state.pending[0]
  view.selectedBot = 2
  expect(oldRead.options.signal.aborted).toBe(true)
  oldRead.resolve({ data: { credentials: [{ id: 'fictional-old-scope' }] } })
  await flush()
  expect(view.snapshot).toBe(null)
})
