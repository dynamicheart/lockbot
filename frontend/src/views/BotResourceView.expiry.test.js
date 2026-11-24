import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ref, ssrContextKey } from 'vue'
import BotResourceView from './BotResourceView.vue'

const panel = vi.hoisted(() => ({ personalPanelWebDisabled: false }))
vi.mock('../utils/personalPanelWeb', () => panel)
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
  panel.personalPanelWebDisabled = false
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
async function mount(expiresAt, { addressDenied = false } = {}) {
  const payload = {
    name: 'Fixture',
    bot_type: 'QUEUE',
    health: addressDenied ? 'running' : 'stopped',
    resources: addressDenied ? [{ id: 'node1' }] : [],
    captured_at: '2026-11-14T04:00:00Z',
    server_time: '2026-11-14T04:00:00Z',
    view_expires_at: expiresAt,
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_, options) =>
      options.method === 'POST'
        ? { status: 403, ok: false }
        : { status: 200, ok: true, json: async () => payload }
    )
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

it.each([
  [false, true],
  [true, false],
])(
  'handles the personal resource link when the retirement flag is %s',
  async (disabled, visible) => {
    panel.personalPanelWebDisabled = disabled
    const view = await mount(null, { addressDenied: true })
    expect(view.addressAuthorizationRequired).toBe(true)
    expect(view.showPersonalPanelLink).toBe(visible)
  }
)

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

it('refreshes every 30 seconds while visible and immediately on return', async () => {
  await mount(null)
  const onVisibilityChange = document.addEventListener.mock.calls.find(
    ([event]) => event === 'visibilitychange'
  )[1]
  expect(fetch).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(29_999)
  expect(fetch).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(fetch).toHaveBeenCalledTimes(2)

  document.hidden = true
  onVisibilityChange()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetch).toHaveBeenCalledTimes(2)

  document.hidden = false
  onVisibilityChange()
  expect(fetch).toHaveBeenCalledTimes(3)

  app.unmount()
  app = null
  await vi.advanceTimersByTimeAsync(30_000)
  expect(fetch).toHaveBeenCalledTimes(3)
  expect(document.removeEventListener).toHaveBeenCalledWith('visibilitychange', onVisibilityChange)
})
