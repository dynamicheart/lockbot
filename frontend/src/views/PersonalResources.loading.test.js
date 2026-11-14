import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import PersonalResources from './PersonalResources.vue'

const renderer = createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert: () => {},
  remove: () => {},
  setText: () => {},
  setElementText: () => {},
  parentNode: () => null,
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
afterEach(() => {
  app?.unmount()
  vi.unstubAllGlobals()
})
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => data })
function mount() {
  app = renderer.createApp({ ...PersonalResources, render: () => null })
  app.provide(ssrContextKey, {})
  app.mount({})
  return app._instance.setupState
}
it('shows the verified session before slow resource reports finish, without claiming ready data', async () => {
  let finish
  const pending = new Promise((resolve) => {
    finish = resolve
  })
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url.endsWith('/session')
        ? Promise.resolve(response({ authenticated: true, identities: [] }))
        : pending
    )
  )
  const state = mount()
  await flush()
  expect(state.initialized).toBe(true)
  expect(state.session.authenticated).toBe(true)
  expect(state.resourceLoading).toBe(true)
  expect(state.resourcesReady).toBe(false)
  finish(response({ groups: [] }))
  await flush()
  expect(state.resourceLoading).toBe(false)
  expect(state.resourcesReady).toBe(true)
})
it('does not load protected reports for an unauthenticated session', async () => {
  const fetch = vi.fn(async () => response({ authenticated: false, identities: [] }))
  vi.stubGlobal('fetch', fetch)
  const state = mount()
  await flush()
  expect(state.initialized).toBe(true)
  expect(state.session.authenticated).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(state.resourcesReady).toBe(false)
})
it('ends resource loading and shows an error when a reporter request fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) =>
      url.endsWith('/session')
        ? response({ authenticated: true, identities: [] })
        : response({}, false)
    )
  )
  const state = mount()
  await flush()
  expect(state.initialized).toBe(true)
  await vi.waitFor(() => expect(state.error).toContain('无法更新资源'))
  expect(state.resourceLoading).toBe(false)
  expect(state.resourcesReady).toBe(false)
})

it('separates login and report sources and excludes another identity', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ authenticated: false, identities: [] }))
  )
  const state = mount()
  await flush()
  const identity = {
    issuer: 'https://service.example.invalid',
    platform: 'infoflow',
    realm: 'one',
    subject: 'alice',
  }
  state.groups = [
    { label: 'Native', identity, source: { kind: 'identity_provider', label: '登录服务' } },
    { label: 'Shared', identity, source: { kind: 'reporter', label: '共享报表' } },
    { label: 'Legacy', identity },
    { label: 'Other user', identity: { ...identity, subject: 'bob' } },
  ]
  expect(state.identitySources(identity)).toEqual([
    { kind: 'identity_provider', label: '登录服务', groups: ['Native'] },
    { kind: 'reporter', label: '共享报表', groups: ['Shared'] },
    { kind: 'unknown', label: '资源来源待确认', groups: ['Legacy'] },
  ])
})

const identity = {
  issuer: 'https://service.example.invalid',
  platform: 'infoflow',
  realm: 'fixture',
  subject: 'alice',
  scope: 'bot-a',
}
const confirmed = {
  label: 'Fixture',
  identity,
  status: 'ok',
  allocations: [{ kind: 'hold', node: 'node-a', device: '0' }],
}
async function readyGroups(groups) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) =>
      url.endsWith('/session')
        ? response({ authenticated: true, identities: [identity] })
        : response({ groups })
    )
  )
  const state = mount()
  await flush()
  await vi.waitFor(() => expect(state.resourcesReady).toBe(true))
  return state
}

it('uses loading placeholders initially and shows a confirmed zero after an empty response', async () => {
  let finish
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url.endsWith('/session')
        ? Promise.resolve(response({ authenticated: true, identities: [identity] }))
        : new Promise((resolve) => {
            finish = resolve
          })
    )
  )
  const state = mount()
  await flush()
  expect(state.initialResourceLoading).toBe(true)
  expect(state.statistics.every((item) => item.value === null)).toBe(true)
  finish(response({ groups: [{ ...confirmed, allocations: [] }] }))
  await flush()
  expect(state.initialResourceLoading).toBe(false)
  expect(state.statistics.map((item) => item.value)).toEqual([0, 0, 0])
})

it('keeps unavailable group counts unknown rather than showing zero or em dashes', async () => {
  const group = { ...confirmed, status: 'unknown' }
  const state = await readyGroups([group])
  expect(state.statistics.map((item) => item.value)).toEqual([null, null, null])
  expect(state.groupCounts(group).map((item) => item.value)).toEqual([null, null])
  expect(state.pendingGroups).toBe(1)
  expect(state.knownGroups).toHaveLength(0)
})

it('retains prior confirmed counts while refreshing the same verified scope, then marks failures unknown', async () => {
  const state = await readyGroups([confirmed])
  let finish
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url.endsWith('/session')
        ? Promise.resolve(response({ authenticated: true, identities: [identity] }))
        : new Promise((resolve) => {
            finish = resolve
          })
    )
  )
  const pending = state.refresh().catch(() => {})
  await flush()
  expect(state.resourceLoading).toBe(true)
  expect(state.initialResourceLoading).toBe(false)
  expect(state.statistics.map((item) => item.value)).toEqual([1, 1, 0])
  finish(response({}, false))
  await pending
  expect(state.resourceLoading).toBe(false)
  expect(state.groups).toHaveLength(1)
  expect(state.groups[0].status).toBe('unknown')
  expect(state.statistics.map((item) => item.value)).toEqual([null, null, null])
})

it('clears retained data when session validation fails', async () => {
  const state = await readyGroups([confirmed])
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({}, false))
  )
  await state.refresh().catch(() => {})
  expect(state.groups).toEqual([])
  expect(state.resourcesReady).toBe(false)
  expect(state.statistics.every((item) => item.value === null)).toBe(true)
})

it('clears previous data when the verified session changes scope', async () => {
  const state = await readyGroups([confirmed])
  let finish
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url.endsWith('/session')
        ? Promise.resolve(
            response({ authenticated: true, identities: [{ ...identity, scope: 'bot-b' }] })
          )
        : new Promise((resolve) => {
            finish = resolve
          })
    )
  )
  const pending = state.refresh()
  await flush()
  expect(state.groups).toEqual([])
  expect(state.initialResourceLoading).toBe(true)
  finish(response({ groups: [] }))
  await pending
})

it('does not restore an old resource response after logout', async () => {
  const state = await readyGroups([confirmed])
  let finish
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url.endsWith('/session')
        ? Promise.resolve(response({ authenticated: true, identities: [identity] }))
        : url.endsWith('/logout')
          ? Promise.resolve(response({}))
          : new Promise((resolve) => {
              finish = resolve
            })
    )
  )
  const pending = state.refresh()
  await flush()
  await state.logout()
  finish(response({ groups: [confirmed] }))
  await pending
  expect(state.session.authenticated).toBe(false)
  expect(state.groups).toEqual([])
  expect(state.resourcesReady).toBe(false)
})
