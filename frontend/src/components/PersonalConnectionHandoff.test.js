import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import Handoff from './PersonalConnectionHandoff.vue'

const ORIGIN = 'https://panel.example.test'
const SECRET = 'lbr_SYNTHETIC_SECRET_NOT_FOR_THE_DEFAULT_PAGE'
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: () => ({ text: '' }),
  insert(node, parent) {
    node.parent = parent
    parent.children.push(node)
  },
  remove(node) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
  },
  setText: (node, text) => (node.text = text),
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: () => null,
  patchProp: (node, key, _previous, value) => (node.props[key] = value),
})
const skill = () => ({
  skill_id: 'lockbot-resource-agent',
  bundle_url: ORIGIN + '/api/agent-skill/lockbot-resource-agent.zip',
  markdown_url: ORIGIN + '/api/agent-skill/SKILL.md',
  manifest_url: ORIGIN + '/api/agent-skill',
})
const guide = () =>
  `请先读取 ${skill().bundle_url} 和 ${skill().markdown_url}，保留有效原 Key；只检查状态。`
const capabilities = () => ({
  connection_management: {
    version: 1,
    refresh_semantics: 'revalidate_only',
    skill: skill(),
    recovery_instructions: guide(),
    credential_disclosure_supported: false,
  },
})
const row = (name = 'Group one') => ({
  id: 'a'.repeat(32),
  service_origin: ORIGIN,
  instance_id: 'fixture-instance',
  bot: { id: 7, uuid: 'fixture-bot-uuid', name, type: 'NODE' },
  actor: { platform: 'infoflow', external_user_id: 'alice' },
  identity: {
    issuer: 'not-an-installation-origin',
    platform: 'infoflow',
    realm: 'realm',
    subject: 'alice',
    scope: 'scope',
  },
  expires_at: null,
  credential_recovery_health: 'legacy_missing',
  instructions: `请安装 ${skill().bundle_url}，正文 ${skill().markdown_url}。连接 ${name} / alice / fixture-bot-uuid。Key 单独私下提供。`,
})
const list = (rows = [row()]) => ({
  schema_version: 1,
  connections: rows,
  some_authorizations_unavailable: false,
})
const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => data })
const flush = async () => {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve()
    await nextTick()
  }
}
let app, state, root, fetchMock, clipboard
function mount(props = {}) {
  app = renderer.createApp(Handoff, { authenticated: true, scopeKey: 'alice-scope', ...props })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  root = { children: [] }
  app.mount(root)
  state = app._instance.setupState
}
function rendered() {
  // Node-mode SFC imports use ssrRender. Render the already-loaded setup state
  // explicitly so markup assertions exercise the real template, not a stub.
  return renderToString(createSSRApp({ ...Handoff, setup: () => state }, { authenticated: true }))
}
beforeEach(() => {
  vi.stubGlobal('location', { origin: ORIGIN })
  clipboard = vi.fn(async () => {})
  vi.stubGlobal('navigator', { clipboard: { writeText: clipboard } })
  fetchMock = vi.fn(async (url) =>
    response(url.endsWith('/capabilities') ? capabilities() : list())
  )
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('copies a deployment-pinned secret-free Skill instruction only on an explicit action', async () => {
  mount()
  await flush()
  expect(state.items).toHaveLength(1)
  expect(clipboard).not.toHaveBeenCalled()
  await state.copyInstructions(state.items[0])
  expect(clipboard).toHaveBeenCalledWith(row().instructions)
  expect(clipboard.mock.calls[0][0]).toContain(skill().bundle_url)
  expect(clipboard.mock.calls[0][0]).toContain(skill().markdown_url)
  expect(clipboard.mock.calls[0][0]).not.toContain('not-an-installation-origin')
  for (const [, options] of fetchMock.mock.calls)
    expect(options).toMatchObject({
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
    })
})
it('projects metadata and never retains unexpected key fields in default state or DOM', async () => {
  fetchMock.mockImplementation(async (url) =>
    response(
      url.endsWith('/capabilities')
        ? capabilities()
        : list([{ ...row(), credential: SECRET, encrypted: SECRET }])
    )
  )
  mount()
  await flush()
  expect(JSON.stringify(state.items)).not.toContain(SECRET)
  const markup = await rendered()
  expect(markup).toContain('Group one')
  expect(markup).not.toContain(SECRET)
  expect(clipboard).not.toHaveBeenCalled()
})
it('keeps recovery guidance available without requesting protected data for a lost session', async () => {
  mount({ authenticated: false })
  await flush()
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/personal-panel/capabilities'])
  expect(state.items).toEqual([])
  await state.copyRecovery()
  expect(clipboard).toHaveBeenCalledWith(guide())
})
it('status refresh makes only read requests and preserves a missing-copy explanation', async () => {
  mount()
  await flush()
  await state.refresh()
  expect(state.healthText(state.items[0].credential_recovery_health)).toContain('尚无恢复副本')
  expect(fetchMock).toHaveBeenCalledTimes(4)
  expect(
    fetchMock.mock.calls.every(
      ([, options]) => !options.method && !options.body && !options.headers
    )
  ).toBe(true)
  expect(clipboard).not.toHaveBeenCalled()
})
it.each([401, 403])(
  'denied metadata status %s clears rows and offers a fixed Agent recovery guide',
  async (status) => {
    mount()
    await flush()
    fetchMock.mockImplementation(async (url) =>
      response(
        url.endsWith('/capabilities') ? capabilities() : {},
        url.endsWith('/capabilities') ? 200 : status
      )
    )
    await state.refresh()
    expect(state.items).toEqual([])
    expect(state.error).toContain('当前绑定已失效或不可用')
    await state.copyRecovery()
    expect(clipboard).toHaveBeenCalledWith(guide())
  }
)
it('does not infer native credential authority when an identity provider lacks the capability', async () => {
  fetchMock.mockResolvedValue(response({ protocol: 'lockbot.personal-panel.v1' }))
  mount()
  await flush()
  expect(state.supported).toBe(false)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(state.items).toEqual([])
  expect(await rendered()).toContain('此来源仅支持资源查询，凭证请由原 Agent 管理。')
})
it.each([
  'https://different.example.test/api/agent-skill/lockbot-resource-agent.zip',
  'https://user:secret@panel.example.test/api/agent-skill/lockbot-resource-agent.zip',
])('rejects an unapproved installation URL %s', async (url) => {
  const caps = capabilities()
  caps.connection_management.skill.bundle_url = url
  fetchMock.mockResolvedValue(response(caps))
  mount()
  await flush()
  expect(state.error).toContain('无法确认当前连接')
  expect(state.items).toEqual([])
  expect(fetchMock).toHaveBeenCalledTimes(1)
})
it('aborts a changed identity scope and discards an old connection response', async () => {
  let finish, oldSignal
  fetchMock
    .mockImplementationOnce(async () => response(capabilities()))
    .mockImplementationOnce((_url, options) => {
      oldSignal = options.signal
      return new Promise((resolve) => {
        finish = resolve
      })
    })
  mount()
  await flush()
  app._instance.props.scopeKey = 'new-scope'
  await flush()
  expect(oldSignal.aborted).toBe(true)
  finish(response(list([row('Late old group')])))
  await flush()
  expect(JSON.stringify(state.items)).not.toContain('Late old group')
})
it('discards a late response after the component is removed', async () => {
  let finish, oldSignal
  fetchMock
    .mockImplementationOnce(async () => response(capabilities()))
    .mockImplementationOnce((_url, options) => {
      oldSignal = options.signal
      return new Promise((resolve) => {
        finish = resolve
      })
    })
  mount()
  await flush()
  app.unmount()
  app = null
  finish(response(list()))
  await flush()
  expect(oldSignal.aborted).toBe(true)
  expect(state.items).toEqual([])
})

it('copies only a public instruction through the private-HTTP clipboard fallback and removes its temporary node', async () => {
  vi.stubGlobal('navigator', {})
  const element = { style: {}, select: vi.fn() }
  const document = {
    createElement: vi.fn(() => element),
    body: { appendChild: vi.fn(), removeChild: vi.fn() },
    execCommand: vi.fn(() => true),
  }
  vi.stubGlobal('document', document)
  mount()
  await flush()
  await state.copyRecovery()
  expect(element.value).toBe(guide())
  expect(element.value).not.toContain(SECRET)
  expect(document.execCommand).toHaveBeenCalledWith('copy')
  expect(document.body.removeChild).toHaveBeenCalledWith(element)
  expect(state.feedback).toContain('已复制无 Key 说明')
})
