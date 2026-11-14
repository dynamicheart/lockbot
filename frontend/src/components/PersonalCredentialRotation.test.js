import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import Rotation from './PersonalCredentialRotation.vue'
import { pendingCredentialRotationTicket } from '../utils/personalPanelTicket'

const KEY = 'lbr_SYNTHETIC_NEW_KEY_NOT_A_REAL_CREDENTIAL'
const ORIGIN = 'https://panel.example.test'
const connection = {
  bot: { name: 'Fixture group', uuid: 'fixture-bot' },
  actor: { platform: 'infoflow', external_user_id: 'alice' },
  instance_id: 'fixture-instance',
}
const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => data })
const preview = () => ({
  purpose: 'rotate_current_key',
  connection,
  intent: 'synthetic-server-intent',
  expires_in: 120,
  service_origin: ORIGIN,
})
const completed = () => ({
  purpose: 'rotated_current_key',
  credential: KEY,
  requires_fresh_login: true,
  expires_in: 120,
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
    { ...Rotation, render: () => null },
    { authenticated: true, scopeKey: 'alice', ...props }
  )
  app.provide(ssrContextKey, {})
  app.mount({})
  state = app._instance.setupState
}
async function begin(props) {
  mount(props)
  pendingCredentialRotationTicket.value = 'synthetic-rotate-link'
  await flush()
}
const markup = () =>
  renderToString(createSSRApp({ ...Rotation, setup: () => state }, { authenticated: true }))
function acknowledge() {
  state.typedGroup = connection.bot.name
  state.acknowledged = true
}
beforeEach(() => {
  pendingCredentialRotationTicket.value = ''
  events = {}
  vi.stubGlobal('location', { origin: ORIGIN })
  vi.stubGlobal('window', {
    addEventListener: (name, cb) => (events[name] = cb),
    removeEventListener: vi.fn(),
  })
  vi.stubGlobal('document', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
  clipboard = vi.fn(async () => {})
  vi.stubGlobal('navigator', { clipboard: { writeText: clipboard } })
  fetchMock = vi.fn(async (url) => response(url.endsWith('-preview') ? preview() : completed()))
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  app?.unmount()
  app = null
  pendingCredentialRotationTicket.value = ''
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('previews impact without resetting and enforces typed name plus a second confirmation', async () => {
  await begin()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(state.credential).toBe('')
  expect(await markup()).toContain('所有 Agent 都立即失效')
  expect(await markup()).not.toContain(KEY)
  await state.confirm()
  state.typedGroup = connection.bot.name
  await state.confirm()
  state.typedGroup = 'wrong group'
  state.acknowledged = true
  await state.confirm()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  acknowledge()
  await state.confirm()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  const [url, options] = fetchMock.mock.calls[1]
  expect(url).toBe('/api/personal-panel/credential-rotation-confirm')
  expect(JSON.parse(options.body)).toEqual({
    intent: 'synthetic-server-intent',
    typed_group: connection.bot.name,
    confirmation: 'invalidate_all_agents_using_this_key',
  })
  expect(state.credential).toBe(KEY)
  expect(clipboard).not.toHaveBeenCalled()
  await state.copyKey()
  expect(clipboard).toHaveBeenCalledExactlyOnceWith(KEY)
  state.close()
  expect(state.credential).toBe('')
  expect(await markup()).not.toContain(KEY)
})
it('does not issue a browser request without the current personal session', async () => {
  await begin({ authenticated: false })
  expect(fetchMock).not.toHaveBeenCalled()
  expect(state.error).toContain('需要本人当前已确认')
})
it('does not retry an unknown rotation result and never displays backend error content', async () => {
  await begin()
  fetchMock.mockResolvedValue(response({ detail: KEY }, 503))
  acknowledge()
  await state.confirm()
  await state.confirm()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(state.error).toContain('服务端可能已完成')
  expect(state.error).not.toContain(KEY)
  expect(state.credential).toBe('')
})
it.each(['scope', 'logout', 'close', 'unmount'])(
  'clears state and discards a late success after %s',
  async (action) => {
    await begin()
    let finish
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)))
    acknowledge()
    const pending = state.confirm()
    if (action === 'scope') app._instance.props.scopeKey = 'bob'
    if (action === 'logout') app._instance.props.authenticated = false
    if (action === 'close') state.close()
    if (action === 'unmount') {
      app.unmount()
      app = null
    }
    finish(response(completed()))
    await pending
    expect(state.credential).toBe('')
    expect(clipboard).not.toHaveBeenCalled()
  }
)
it('rejects a success resolved after sleep before focus or timeout callbacks', async () => {
  vi.useFakeTimers()
  await begin()
  let finish
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)))
  acknowledge()
  const pending = state.confirm()
  vi.setSystemTime(Date.now() + 16000)
  finish(response(completed()))
  await pending
  expect(state.credential).toBe('')
  expect(state.error).toContain('不要重复点击或自动重试')
})
it('expires the intent and cannot submit after a sleeping browser resumes', async () => {
  vi.useFakeTimers()
  await begin()
  acknowledge()
  vi.setSystemTime(Date.now() + 121000)
  await state.confirm()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(state.credential).toBe('')
  expect(state.error).toContain('已过期')
})
it('clears the displayed new Key on focus after expiry', async () => {
  vi.useFakeTimers()
  await begin()
  acknowledge()
  await state.confirm()
  vi.setSystemTime(Date.now() + 121000)
  events.focus()
  expect(state.credential).toBe('')
  await state.copyKey()
  expect(clipboard).not.toHaveBeenCalled()
})
it('rejects a preview from an unexpected service and never confirms it', async () => {
  fetchMock.mockResolvedValue(
    response({ ...preview(), service_origin: 'https://other.example.test' })
  )
  await begin()
  acknowledge()
  await state.confirm()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(state.preview).toBe(null)
})
