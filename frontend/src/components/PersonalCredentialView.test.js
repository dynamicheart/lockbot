import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import View from './PersonalCredentialView.vue'
import { pendingCredentialViewTicket } from '../utils/personalPanelTicket'

const KEY = 'lbr_SYNTHETIC_TEST_KEY_NOT_A_REAL_CREDENTIAL'
const proof = 'synthetic-single-purpose-view-ticket'
const connection = {
  bot: { name: 'Fixture group', uuid: 'fixture-bot' },
  actor: { platform: 'infoflow', external_user_id: 'alice' },
  instance_id: 'fixture-instance',
  credential_recovery_health: 'recoverable',
}
const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => data })
const authority = () => ({
  purpose: 'view_current_key',
  expires_in: 120,
  expires_at: Date.now() / 1000 + 120,
})
const renderer = createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert() {},
  remove() {},
  setText() {},
  setElementText() {},
  parentNode: () => null,
  nextSibling: () => null,
  patchProp() {},
})
let app, state, fetchMock, clipboard, events
const flush = async () => {
  for (let i = 0; i < 6; i++) {
    await Promise.resolve()
    await nextTick()
  }
}
function mount(props = {}) {
  app = renderer.createApp(
    { ...View, render: () => null },
    { authenticated: true, scopeKey: 'alice', ...props }
  )
  app.provide(ssrContextKey, {})
  app.mount({})
  state = app._instance.setupState
}
const markup = () =>
  renderToString(createSSRApp({ ...View, setup: () => state }, { authenticated: true }))
async function begin(props) {
  mount(props)
  pendingCredentialViewTicket.value = proof
  await flush()
}
beforeEach(() => {
  pendingCredentialViewTicket.value = ''
  events = {}
  vi.stubGlobal('window', {
    addEventListener: (name, cb) => (events[name] = cb),
    removeEventListener: vi.fn(),
  })
  clipboard = vi.fn(async () => {})
  vi.stubGlobal('navigator', { clipboard: { writeText: clipboard } })
  fetchMock = vi.fn(async (url) =>
    response(
      url.endsWith('-preview')
        ? { ...authority(), connection, credential: KEY }
        : { ...authority(), credential: KEY }
    )
  )
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  app?.unmount()
  app = null
  pendingCredentialViewTicket.value = ''
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('previews only public fields and requires an explicit reveal followed by an explicit copy', async () => {
  await begin()
  expect(pendingCredentialViewTicket.value).toBe('')
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(state.credential).toBe('')
  expect(JSON.stringify(state.preview)).not.toContain(KEY)
  expect(await markup()).toContain('Fixture group')
  expect(await markup()).not.toContain(KEY)
  expect(clipboard).not.toHaveBeenCalled()
  await state.reveal()
  expect(state.credential).toBe(KEY)
  expect(await markup()).toContain(KEY)
  expect(clipboard).not.toHaveBeenCalled()
  await state.copyKey()
  expect(clipboard).toHaveBeenCalledExactlyOnceWith(KEY)
  state.close()
  expect(state.credential).toBe('')
  expect(await markup()).not.toContain(KEY)
  for (const [url, options] of fetchMock.mock.calls) {
    expect(url).not.toContain(proof)
    expect(options).toMatchObject({
      method: 'POST',
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
    })
  }
})
it('does not exchange a view ticket when the current browser has no personal session', async () => {
  await begin({ authenticated: false })
  expect(fetchMock).not.toHaveBeenCalled()
  expect(state.error).toContain('只读登录不授予查看权限')
  expect(state.credential).toBe('')
})
it('never retries a consumed view proof after a failure', async () => {
  await begin()
  fetchMock.mockResolvedValue(response({ detail: KEY }, 403))
  await state.reveal()
  await state.reveal()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(state.error).toContain('链接不能重复使用')
  expect(state.error).not.toContain(KEY)
  expect(state.credential).toBe('')
})
it.each(['scope', 'logout', 'close', 'unmount'])(
  'clears an explicit value and ignores a late response after %s',
  async (action) => {
    await begin()
    let finish
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)))
    const pending = state.reveal()
    if (action === 'scope') app._instance.props.scopeKey = 'bob'
    if (action === 'logout') app._instance.props.authenticated = false
    if (action === 'close') state.close()
    if (action === 'unmount') {
      app.unmount()
      app = null
    }
    finish(response({ ...authority(), credential: KEY }))
    await pending
    expect(state.credential).toBe('')
    expect(clipboard).not.toHaveBeenCalled()
  }
)
it('rejects a reveal resolved after sleep before any focus event or timer callback', async () => {
  vi.useFakeTimers()
  await begin()
  let finish
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)))
  const pending = state.reveal()
  vi.setSystemTime(Date.now() + 16000)
  finish(response({ ...authority(), credential: KEY }))
  await pending
  expect(state.credential).toBe('')
  expect(state.error).toContain('本次查看未完成')
})
it('clears an already displayed Key on expiry and on focus after sleeping', async () => {
  vi.useFakeTimers()
  await begin()
  await state.reveal()
  expect(state.credential).toBe(KEY)
  vi.setSystemTime(Date.now() + 121000)
  events.focus()
  expect(state.credential).toBe('')
  await state.copyKey()
  expect(clipboard).not.toHaveBeenCalled()
  expect(state.error).toContain('已过期')
})
it('expires a displayed Key without a user action', async () => {
  vi.useFakeTimers()
  await begin()
  await state.reveal()
  await vi.advanceTimersByTimeAsync(120001)
  expect(state.credential).toBe('')
})
it('allows only one reveal request even when confirm is clicked twice', async () => {
  await begin()
  let finish
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)))
  const pending = state.reveal()
  await state.reveal()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  finish(response({ ...authority(), credential: KEY }))
  await pending
})
