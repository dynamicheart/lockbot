/* eslint-disable vue/one-component-per-file -- Slot stubs exercise panel controls. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ssrContextKey, createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import Manager from './AuditCredentialManager.vue'
import api from '../utils/api'
const copy = vi.hoisted(() => vi.fn())
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() } }))
vi.mock('../utils/helpers', () => ({ useHelpers: () => ({ copyText: copy }) }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: () => ({ text: '' }),
  insert(child, parent, anchor) {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove(child) {
    const index = child.parent?.children.indexOf(child)
    if (index >= 0) child.parent.children.splice(index, 1)
  },
  setText: (node, text) => (node.text = text),
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: (node, key, _previous, value) => (node.props[key] = value),
})
const Container = defineComponent({
  setup:
    (_, { slots }) =>
    () =>
      h('div', [slots.label?.(), slots.default?.(), slots.footer?.()]),
})
const Button = defineComponent({
  props: { disabled: Boolean },
  setup:
    (props, { slots }) =>
    () =>
      h('button', { disabled: props.disabled }, slots.default?.()),
})
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
let app, state, gets, writes
const ID = 'a'.repeat(22)
const SECRET = `lba1.${ID}.${'b'.repeat(43)}`
const row = () => ({
  id: ID,
  name: 'Approved consumer',
  scope: 'task_notes:read',
  bot_ids: [4],
  expires_at: '2099-01-01T00:00:00Z',
  revoked_at: null,
})
function mount(props = {}) {
  app = renderer.createApp(Manager, { authorized: true, principalId: 7, ...props })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.directive('loading', {})
  for (const name of [
    'ElForm',
    'ElFormItem',
    'ElSelect',
    'ElOption',
    'ElInput',
    'ElTable',
    'ElTableColumn',
    'ElDialog',
    'ElPopconfirm',
  ])
    app.component(name, Container)
  app.component('ElButton', Button)
  app.mount({ children: [] })
  state = app._instance.setupState
}
function deferred(list, path, options, body) {
  return new Promise((resolve, reject) => list.push({ path, options, body, resolve, reject }))
}
async function finishLoad(rows = []) {
  gets.at(-2).resolve({ data: { items: rows } })
  gets.at(-1).resolve({ data: [{ id: 4, name: 'Group', secret: 'DO_NOT_RETAIN' }] })
  await flush()
}
function fill() {
  state.name = 'Approved consumer'
  state.botIds = [4]
  state.ttl = 600
}
beforeEach(() => {
  gets = []
  writes = []
  vi.clearAllMocks()
  api.get.mockImplementation((path, options) => deferred(gets, path, options))
  api.post.mockImplementation((path, body, options) => deferred(writes, path, options, body))
  api.delete.mockImplementation((path, options) => deferred(writes, path, options))
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
})
it('does not fetch or mutate without current authorization', async () => {
  mount({ authorized: false })
  fill()
  await state.create()
  await state.revoke(ID)
  expect(gets).toEqual([])
  expect(writes).toEqual([])
})
it('lists safe metadata only and never issues automatically', async () => {
  mount()
  expect(gets[0].path).toBe('/admin/audit-credentials')
  expect(gets[0].options).toMatchObject({ params: { limit: 50, offset: 0 }, _silent: true })
  expect(gets[1].path).toBe('/admin/bots')
  await finishLoad([{ ...row(), credential: SECRET }])
  expect(state.items).toEqual([row()])
  expect(state.bots).toEqual([{ id: 4, name: 'Group' }])
  expect(writes).toEqual([])
  expect(state.ttl).toBe(86400)
})
it('explicitly issues a 600-second scoped grant and reveals/copies it only once', async () => {
  mount()
  await finishLoad()
  fill()
  const pending = state.create()
  expect(writes[0].body).toEqual({
    name: 'Approved consumer',
    bot_ids: [4],
    expires_in_seconds: 600,
  })
  expect(writes[0].options._silent).toBe(true)
  expect(copy).not.toHaveBeenCalled()
  writes[0].resolve({ data: { ...row(), credential: SECRET, shown_once: true } })
  await flush()
  await finishLoad([row()])
  await pending
  expect(state.secret).toBe(SECRET)
  expect(JSON.stringify(state.items)).not.toContain(SECRET)
  state.copySecret()
  expect(copy).toHaveBeenCalledWith(SECRET)
  state.clearSecret()
  expect(state.secret).toBe('')
})
it('refuses unknown bots, duplicate IDs and unsupported TTL', async () => {
  mount()
  await finishLoad()
  for (const change of [
    { botIds: [99] },
    { botIds: [4, 4] },
    { name: ' ' },
    { ttl: 0 },
    { ttl: 604801 },
  ]) {
    fill()
    Object.assign(state, change)
    await state.create()
  }
  expect(writes).toEqual([])
})
it.each([401, 403])('clears transient secrets and locks down on server %s', async (code) => {
  mount()
  await finishLoad([row()])
  const pending = state.load()
  state.secret = SECRET
  gets.at(-2).reject({ response: { status: code, data: { detail: SECRET } } })
  gets.at(-1).resolve({ data: [] })
  await pending
  expect(state.secret).toBe('')
  expect(state.items).toEqual([])
  expect(state.bots).toEqual([])
  expect(state.denied).toBe(true)
  expect(state.error).not.toContain(SECRET)
  fill()
  await state.create()
  expect(writes).toEqual([])
})
it('discards a late one-time secret after account switch and unmount', async () => {
  mount()
  await finishLoad()
  fill()
  const old = state.create()
  app._instance.props.principalId = 8
  await flush()
  expect(writes[0].options.signal.aborted).toBe(true)
  writes[0].resolve({ data: { ...row(), credential: SECRET, shown_once: true } })
  await old
  expect(state.secret).toBe('')
  await finishLoad()
  fill()
  const next = state.create()
  app.unmount()
  app = null
  writes[1].resolve({ data: { ...row(), credential: SECRET, shown_once: true } })
  await next
  expect(state.secret).toBe('')
})
it('revokes only a current listed credential and reloads safe metadata', async () => {
  mount()
  await finishLoad([row()])
  await state.revoke('other')
  expect(writes).toEqual([])
  const pending = state.revoke(ID)
  expect(writes[0].path).toBe(`/admin/audit-credentials/${ID}`)
  expect(api.delete).toHaveBeenCalledOnce()
  writes[0].resolve({})
  await flush()
  await finishLoad([{ ...row(), revoked_at: '2026-01-01T00:00:00Z' }])
  await pending
  expect(state.status(state.items[0])).toBe('revoked')
})
it('never retries uncertain issuance or renders raw API details', async () => {
  mount()
  await finishLoad()
  fill()
  const pending = state.create()
  writes[0].reject({ response: { status: 500, data: { detail: SECRET } } })
  await pending
  expect(state.error).toBe('auditCredentials.mutationFailed')
  expect(state.secret).toBe('')
  expect(writes).toHaveLength(1)
})
it('ignores stale page results', async () => {
  mount()
  await finishLoad()
  const previous = state.load()
  const old = gets.at(-2)
  state.offset = 50
  const pending = state.load()
  expect(old.options.signal.aborted).toBe(true)
  old.resolve({ data: { items: [row()] } })
  gets.at(-3).resolve({ data: [] })
  await previous
  await finishLoad()
  await pending
  expect(state.items).toEqual([])
  expect(state.offset).toBe(50)
})
it('renders no management controls for unauthorized users', async () => {
  const shell = createSSRApp(Manager, { authorized: false, principalId: 7 })
  shell.config.warnHandler = () => {}
  shell.config.globalProperties.$t = (key) => key
  expect(await renderToString(shell)).not.toContain('auditCredentials.create')
  expect(writes).toEqual([])
})

it('clears a displayed credential immediately when role authorization is lost', async () => {
  mount()
  await finishLoad()
  state.secret = SECRET
  app._instance.props.authorized = false
  await flush()
  expect(state.secret).toBe('')
  expect(state.items).toEqual([])
  state.copySecret()
  expect(copy).not.toHaveBeenCalled()
  fill()
  await state.create()
  expect(writes).toEqual([])
})

it('rejects a one-time response for another scope without disclosing or retrying', async () => {
  mount()
  await finishLoad()
  fill()
  const pending = state.create()
  writes[0].resolve({ data: { ...row(), bot_ids: [7], credential: SECRET, shown_once: true } })
  await pending
  expect(state.secret).toBe('')
  expect(state.error).toBe('auditCredentials.mutationFailed')
  expect(writes).toHaveLength(1)
})

it('prevents refresh and pagination while issuance or one-time disclosure is pending', async () => {
  mount()
  await finishLoad()
  fill()
  const pending = state.create()
  const reads = gets.length
  state.changePage(1)
  await state.load()
  expect(state.offset).toBe(0)
  expect(gets).toHaveLength(reads)
  writes[0].resolve({ data: { ...row(), credential: SECRET, shown_once: true } })
  await flush()
  await finishLoad([row()])
  await pending
  const completedReads = gets.length
  state.changePage(1)
  await state.load()
  expect(state.secret).toBe(SECRET)
  expect(state.offset).toBe(0)
  expect(gets).toHaveLength(completedReads)
  state.clearSecret()
  const refreshed = state.load()
  await finishLoad([row()])
  await refreshed
  const revoked = state.revoke(ID)
  const revocationReads = gets.length
  state.changePage(1)
  await state.load()
  expect(gets).toHaveLength(revocationReads)
  writes[1].resolve({})
  await flush()
  await finishLoad([])
  await revoked
})

it('updates expiry status locally without polling the service', async () => {
  vi.useFakeTimers()
  mount()
  await finishLoad()
  const credential = { ...row(), expires_at: new Date(Date.now() + 1000).toISOString() }
  expect(state.status(credential)).toBe('active')
  const reads = gets.length
  vi.advanceTimersByTime(30000)
  expect(state.status(credential)).toBe('expired')
  expect(gets).toHaveLength(reads)
})
