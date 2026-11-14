import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, h, reactive, ssrContextKey } from 'vue'
import ResourceUtilization from './ResourceUtilization.vue'
import { prometheusDatasource, validateConfig } from '../utils/utilization/prometheus'
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: { value: 'en' } }) }))
vi.mock('../utils/utilization/prometheus', async (importOriginal) => {
  const original = await importOriginal()
  return {
    ...original,
    prometheusDatasource: {
      ...original.prometheusDatasource,
      query: vi.fn(async () => ({ cards: [] })),
    },
  }
})
// The real setup/watch/lifecycle executes; DOM rendering is irrelevant to request cancellation.
ResourceUtilization.render = () => null
const renderer = createRenderer({
  createComment: () => ({}),
  insert: (node, parent) => {
    node.parent = parent
    parent.children.push(node)
  },
  remove: (node) => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
  },
  parentNode: (node) => node.parent,
  nextSibling: () => null,
})
let app, documentListeners, windowListeners, observed
const source = () =>
  validateConfig({
    id: 'test',
    profile: 'lockbot-v1',
    accessMode: 'short_lived',
    endpoint: 'https://metrics.example.invalid',
    nodeMapping: { node1: 'host1' },
  })
const grant = (token) => ({ token, scope: 'metrics:read', expires_at: Date.now() / 1000 + 300 })
function mount() {
  let serial = 0
  const props = reactive({
    botId: 1,
    scopeKey: 'view-code-one',
    source: source(),
    nodes: ['node1'],
    allowed: true,
    authorize: vi.fn(async () => grant(`metrics-${++serial}`)),
  })
  observed = []
  app = renderer.createApp({
    render: () => h(ResourceUtilization, { ...props, onChange: (value) => observed.push(value) }),
  })
  app.provide(ssrContextKey, {})
  app.mount({ children: [] })
  return props
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-14T00:00:00Z'))
  documentListeners = {}
  windowListeners = {}
  vi.stubGlobal('document', {
    hidden: false,
    addEventListener: (key, fn) => (documentListeners[key] = fn),
    removeEventListener: (key) => delete documentListeners[key],
  })
  vi.stubGlobal('window', {
    addEventListener: (key, fn) => (windowListeners[key] = fn),
    removeEventListener: (key) => delete windowListeners[key],
  })
  prometheusDatasource.query.mockReset().mockResolvedValue({ cards: [] })
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
it('renews automatically after hidden sleep and never polls while hidden', async () => {
  const props = mount()
  await vi.advanceTimersByTimeAsync(0)
  expect(props.authorize).toHaveBeenCalledTimes(1)
  const initial = prometheusDatasource.query.mock.calls[0][2].bearer
  document.hidden = true
  documentListeners.visibilitychange()
  await vi.advanceTimersByTimeAsync(600000)
  expect(prometheusDatasource.query).toHaveBeenCalledTimes(1)
  document.hidden = false
  documentListeners.visibilitychange()
  await vi.advanceTimersByTimeAsync(0)
  expect(props.authorize).toHaveBeenCalledTimes(2)
  expect(prometheusDatasource.query.mock.calls[1][2].bearer).not.toBe(initial)
  expect(observed.at(-1).error).toBeNull()
})
it.each(['bot', 'source', 'code', 'permission'])(
  'aborts and discards old metrics after a %s change',
  async (kind) => {
    let finish, oldSignal
    prometheusDatasource.query.mockImplementationOnce((_source, _nodes, { signal }) => {
      oldSignal = signal
      return new Promise((resolve) => {
        finish = resolve
      })
    })
    const props = mount()
    await vi.advanceTimersByTimeAsync(0)
    if (kind === 'bot') props.botId = 2
    else if (kind === 'source')
      props.source = { ...source(), endpoint: 'https://new.example.invalid' }
    else if (kind === 'code') props.scopeKey = 'replacement-view-code'
    else props.allowed = false
    // Prop updates enter the child during Vue's update flush, where watchers cancel synchronously.
    await vi.advanceTimersByTimeAsync(0)
    expect(oldSignal.aborted).toBe(true)
    finish({ oldScopeSecret: true, cards: [] })
    await vi.advanceTimersByTimeAsync(0)
    expect(observed.some((value) => value.snapshot?.oldScopeSecret)).toBe(false)
    expect(observed.at(-1).snapshot).toBeNull()
  }
)
it('cleans visibility and foreground listeners when the resource panel is removed', async () => {
  mount()
  await vi.advanceTimersByTimeAsync(0)
  expect(windowListeners.focus).toBeTypeOf('function')
  expect(windowListeners.pageshow).toBeTypeOf('function')
  app.unmount()
  app = null
  expect(documentListeners.visibilitychange).toBeUndefined()
  expect(windowListeners.focus).toBeUndefined()
  expect(windowListeners.pageshow).toBeUndefined()
})
