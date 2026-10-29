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

it.each([
  ['activity', AgentActivity],
  ['history', AgentHistory],
  ['authorization audit', BotAudit],
])(
  'clears %s private rows immediately and ignores an old private response after closing consent',
  async (_name, component) => {
    const { view, props } = mount(component)
    expect(state.pending[0].options.params.include_private).toBe(true)
    view.items = [{ purpose: 'fictional-private' }]
    props.includePrivate = false
    await nextTick()
    expect(view.items).toEqual([])
    expect(state.pending.at(-1).options.params.include_private).toBe(false)
    state.pending[0].resolve({ data: { items: [{ purpose: 'fictional-late-private' }], total: 1 } })
    await flush()
    expect(view.items).toEqual([])
  }
)

it('defaults global monitoring to public, pauses private polling and resets consent when filtering another bot', async () => {
  vi.useFakeTimers()
  const { view } = mount(ServiceTrafficPanel, { botId: null })
  expect(state.pending[0].options.params.include_private).toBe(false)
  state.pending[0].resolve({ data: null })
  await flush()
  view.showPrivateTasks = true
  expect(state.pending.at(-1).options.params.include_private).toBe(true)
  const privateRead = state.pending.at(-1)
  privateRead.resolve({ data: null })
  await flush()
  const count = state.pending.length
  await vi.advanceTimersByTimeAsync(16000)
  expect(state.pending.length).toBe(count)
  view.selectedBot = 2
  expect(view.showPrivateTasks).toBe(false)
  expect(view.snapshot).toBe(null)
  expect(state.pending.at(-1).options.params).toMatchObject({ bot_id: 2, include_private: false })
})

it('does not publish a late global private credential snapshot after consent is closed', async () => {
  const { view } = mount(ServiceTrafficPanel, { botId: null })
  state.pending[0].resolve({ data: null })
  await flush()
  view.showPrivateTasks = true
  const privateRead = state.pending.at(-1)
  view.showPrivateTasks = false
  expect(privateRead.options.signal.aborted).toBe(true)
  privateRead.resolve({ data: { credentials: [{ id: 'fictional-private' }] } })
  await flush()
  expect(view.snapshot).toBe(null)
})
