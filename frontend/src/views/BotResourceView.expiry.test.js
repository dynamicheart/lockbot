import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ref, ssrContextKey } from 'vue'
import BotResourceView from './BotResourceView.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '25', code: 'aaaaaaaaaaaaaaaaaaaaaa' }, query: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: ref('zh') }) }))
vi.mock('../components/ResourceUtilization.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ResourceMetricsCell.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ResourceStatusList.vue', () => ({ default: { render: () => null } }))
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
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-14T04:00:00Z'))
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
async function mount(expiresAt) {
  const payload = {
    name: 'Fixture',
    bot_type: 'QUEUE',
    health: 'stopped',
    resources: [],
    captured_at: '2026-11-14T04:00:00Z',
    server_time: '2026-11-14T04:00:00Z',
    view_expires_at: expiresAt,
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ status: 200, ok: true, json: async () => payload }))
  )
  app = renderer.createApp(BotResourceView)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.mount({ children: [] })
  for (let i = 0; i < 4; i++) {
    await Promise.resolve()
    await nextTick()
  }
  return app._instance.setupState
}

it('keeps a permanent server-authorized view after local time advances', async () => {
  const view = await mount(null)
  expect(view.snapshot).not.toBeNull()
  vi.setSystemTime(new Date('2026-12-20T04:00:00Z'))
  await vi.advanceTimersByTimeAsync(1000)
  expect(view.invalid).toBe(false)
  expect(view.snapshot.view_expires_at).toBeNull()
})

it('clears an expired finite view on its local deadline', async () => {
  const view = await mount('2026-11-14T04:00:01Z')
  expect(view.snapshot).not.toBeNull()
  await vi.advanceTimersByTimeAsync(1000)
  expect(view.invalid).toBe(true)
  expect(view.snapshot).toBeNull()
})

it('rejects missing expiry instead of treating it as a permanent link', async () => {
  const view = await mount(undefined)
  expect(view.invalid).toBe(true)
  expect(view.snapshot).toBeNull()
})
