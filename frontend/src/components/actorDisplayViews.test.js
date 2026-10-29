import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ref, ssrContextKey } from 'vue'
import LogViewer from './LogViewer.vue'
import AgentActivity from './AgentActivity.vue'
import AgentHistory from './AgentHistory.vue'
import { actorDisplayKey } from '../utils/useFeishuDisplayNames'
const fixture = vi.hoisted(() => ({ logs: [], reads: vi.fn() }))
vi.mock('../stores/bots', () => ({ useBotsStore: () => ({ getBotLogs: fixture.reads }) }))
vi.mock('../utils/api', () => ({
  default: { get: vi.fn(async () => ({ data: { items: [], total: 0 } })) },
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: { value: 'en' } }) }))
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
function mount(component, provider) {
  app = renderer.createApp(component, { botId: 1, includePrivate: true })
  app.provide(ssrContextKey, { modules: new Set() })
  app.provide(actorDisplayKey, provider)
  app.config.warnHandler = () => {}
  app.directive('loading', {})
  app.mount({ children: [] })
  return app._instance.setupState
}
afterEach(() => {
  app?.unmount()
  app = null
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
it('renders a known CMD actor while downloads retain the original message and stable identity', async () => {
  const original = '[ou_fixture] lock fixture-node 2m'
  fixture.logs = [
    {
      id: 1,
      category: 'command',
      level: 'INFO',
      message: original,
      created_at: '2026-01-01T00:00:00Z',
    },
  ]
  fixture.reads.mockResolvedValue(fixture.logs)
  let downloadBlob
  vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
    downloadBlob = blob
    return 'blob:fixture'
  })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.stubGlobal('document', { createElement: () => ({ click: vi.fn() }) })
  const names = ref({ ou_fixture: 'Display name' })
  const view = mount(LogViewer, { label: (id) => names.value[id] || id })
  await flush()
  expect(view.logDisplay(fixture.logs[0])).toEqual({
    id: 'ou_fixture',
    name: 'Display name',
    rest: ' lock fixture-node 2m',
  })
  expect(fixture.logs[0].message).toBe(original)
  await view.downloadLogs()
  const text = await downloadBlob.text()
  expect(text).toContain(original)
  expect(text).not.toContain('Display name')
  names.value = {}
  expect(view.logDisplay(fixture.logs[0]).name).toBe('ou_fixture')
})
it.each([AgentActivity, AgentHistory])(
  'uses the current injected display map without changing actor data',
  async (component) => {
    const names = ref({ ou_fixture: 'Current name' })
    const view = mount(component, { label: (id) => names.value[id] || id || '—' })
    await flush()
    expect(view.actorNames.label('ou_fixture')).toBe('Current name')
    names.value = {}
    expect(view.actorNames.label('ou_fixture')).toBe('ou_fixture')
  }
)
