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
it('links to the proven service skill and rejects unsafe or credential-bearing URLs', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ authenticated: false, identities: [] }))
  )
  const state = mount()
  await flush()
  expect(state.skillLink('https://service.example.invalid/path')).toEqual({
    service: 'service.example.invalid',
    url: 'https://service.example.invalid/api/agent-skill/SKILL.md',
  })
  for (const issuer of ['javascript:alert(1)', 'https://user:secret@example.invalid', 'invalid']) {
    expect(state.skillLink(issuer)).toBeNull()
  }
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
