import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'
import Handoff from './PersonalConnectionHandoff.vue'
import zhCN from '../i18n/zh-CN.js'

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
const installI18n = (view) =>
  view.use(createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zhCN } }))
function mount(props = {}) {
  app = renderer.createApp(Handoff, { authenticated: true, scopeKey: 'alice-scope', ...props })
  installI18n(app)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  root = { children: [] }
  app.mount(root)
  state = app._instance.setupState
}
function rendered() {
  // Node-mode SFC imports use ssrRender. Render the already-loaded setup state
  // explicitly so markup assertions exercise the real template, not a stub.
  const view = createSSRApp({ ...Handoff, setup: () => state }, { authenticated: true })
  installI18n(view)
  view.component('ElDialog', {
    inheritAttrs: false,
    props: ['modelValue'],
    setup: (props, { slots }) => () => (props.modelValue ? slots.default?.() : null),
  })
  view.component('ElButton', { setup: (_, { slots }) => () => slots.default?.() })
  return renderToString(view)
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

it('lists confirmed credentials directly and copies all connections without keys', async () => {
  const first = row()
  const second = row('Group two')
  second.id = 'b'.repeat(32)
  second.bot = { ...second.bot, id: 8, uuid: 'fixture-second-bot-uuid' }
  const third = { ...row(), id: 'c'.repeat(32) }
  third.actor = { ...third.actor, external_user_id: 'bob' }
  third.identity.subject = 'bob'
  third.instructions = third.instructions.replace('/ alice /', '/ bob /')
  fetchMock.mockImplementation(async (url) =>
    response(url.endsWith('/capabilities') ? capabilities() : list([first, second, third, first]))
  )
  mount()
  await flush()
  expect(state.items).toHaveLength(3)
  const markup = await rendered()
  expect(markup).toContain('Group two')
  expect(markup).not.toContain('个绑定群')
  expect(markup).not.toContain('内容不含 Key')
  expect(markup).toContain('复制全部状态')
  expect(markup).toContain('Agent 接入说明')
  expect(markup).not.toContain('复制接入说明')
  await state.copyAllStatus()
  expect(clipboard).toHaveBeenCalledTimes(1)
  const message = clipboard.mock.calls[0][0]
  expect(message).toContain(ORIGIN + '/#/me')
  expect(message).toContain('Key 状态：需刷新')
  for (const item of [first, second, third]) expect(message).toContain(item.instructions)
  expect(message).not.toContain(SECRET)
})
it('copies only a purpose-specific request to the original Agent when native view authority is advertised', async () => {
  const caps = capabilities()
  caps.connection_management.credential_view = {
    version: 1,
    purpose: 'view_current_key',
    requires_original_credential: true,
    requires_matching_browser_grant: true,
    single_use: true,
    issue_path: '/api/personal-panel/credential-view-links',
  }
  fetchMock.mockImplementation(async (url) =>
    response(url.endsWith('/capabilities') ? caps : list([{ ...row(), credential_recovery_health: 'recoverable' }]))
  )
  mount()
  await flush()
  expect(state.viewSupported).toBe(true)
  expect(await rendered()).toContain('获取 Key 查看链接')
  state.openDetails(state.items[0], true)
  expect(await rendered()).toContain('复制查看请求')
  expect(clipboard).not.toHaveBeenCalled()
  await state.copyViewRequest(state.items[0])
  const message = clipboard.mock.calls[0][0]
  expect(message).toContain('view_current_key')
  expect(message).toContain('Group one')
  expect(message).toContain('不把只读登录或资源链接升级为查看票')
  expect(message).not.toContain(SECRET)
})
it('keeps rotation unavailable by default and copies only an opt-in multi-confirmation request', async () => {
  mount()
  await flush()
  expect(state.rotationSupported).toBe(false)
  await state.copyRotationRequest(state.items[0])
  expect(clipboard).not.toHaveBeenCalled()
  const caps = capabilities()
  caps.connection_management.credential_rotation = {
    version: 1,
    purpose: 'rotate_current_key',
    requires_original_credential: true,
    requires_matching_browser_grant: true,
    single_use: true,
    requires_typed_group: true,
    requires_second_confirmation: true,
    issue_path: '/api/personal-panel/credential-rotation-links',
  }
  fetchMock.mockImplementation(async (url) =>
    response(url.endsWith('/capabilities') ? caps : list())
  )
  await state.refresh()
  expect(state.rotationSupported).toBe(true)
  await state.copyRotationRequest(state.items[0])
  expect(clipboard.mock.calls[0][0]).toContain('输入完整群名并二次确认')
  expect(clipboard.mock.calls[0][0]).toContain('所有 Agent 失效')
  expect(clipboard.mock.calls[0][0]).not.toContain(SECRET)
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
it('shows named and legacy activity for the credential without adding it to copied instructions', async () => {
  const timestamp = '2026-11-14T12:00:00+00:00'
  const activity = {
    window_seconds: 3600,
    recent_active_agents: 1,
    items: [
      { agent_label: '编译 <agent>', last_observed_ip: '2001:db8::1', last_seen_at: timestamp },
      { agent_label: null, last_observed_ip: null, last_seen_at: timestamp },
      { agent_label: SECRET, last_observed_ip: null, last_seen_at: timestamp },
    ],
  }
  fetchMock
    .mockResolvedValueOnce(response(capabilities()))
    .mockResolvedValueOnce(response(list([{ ...row(), agent_activity: activity }])))
  mount()
  await flush()
  expect(state.items[0].agentActivity).toEqual({
    count: 1,
    minutes: 60,
    items: [
      { label: '编译 <agent>', ip: '2001:db8::1', lastSeen: timestamp },
      { label: null, ip: null, lastSeen: timestamp },
    ],
  })
  expect(await rendered()).not.toContain('近 60 分钟 · 1 个活跃')
  state.openDetails(state.items[0])
  const markup = await rendered()
  expect(markup).toContain('近 60 分钟 · 1 个活跃')
  expect(markup).toContain('编译 &lt;agent&gt;')
  expect(markup).toContain('2001:db8::1')
  expect(markup).toContain('未命名客户端')
  expect(markup).not.toContain(SECRET)
  await state.copyInstructions(state.items[0])
  expect(clipboard).toHaveBeenCalledWith(row().instructions)
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
  const markup = await rendered()
  expect(markup).toContain('暂无可管理的机器群 Key')
  expect(markup).not.toContain('Agent 凭证列表')
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

const FOREIGN = 'https://resource.example.test'
const foreignSkill = () => Object.fromEntries(Object.entries(skill()).map(([key, value]) => [key, typeof value === 'string' ? value.replace(ORIGIN, FOREIGN) : value]))
const metadataCaps = () => ({ connection_management: { version: 2, metadata_available: true, refresh_semantics: 'revalidate_only', credential_disclosure_supported: false } })
const metadataList = () => ({ schema_version: 2, service_pins: [{ origin: FOREIGN, instance_id: 'fixture-instance', service_id: 'lockbot.resource-api', api_version: '1' }], connections: [{ ...row(), service_origin: FOREIGN, skill: foreignSkill(), instructions: row().instructions.replaceAll(ORIGIN, FOREIGN), recovery_instructions: guide().replaceAll(ORIGIN, FOREIGN) }] })

it('validates an authenticated metadata-only service pin and copies only its source instructions', async () => {
  fetchMock.mockResolvedValueOnce(response(metadataCaps())).mockResolvedValueOnce(response(metadataList()))
  mount()
  await flush()
  expect(state.items).toHaveLength(1)
  expect(await rendered()).toContain('刷新 Key')
  expect(await rendered()).toContain('复制全部状态')
  state.openDetails(state.items[0], true)
  const help = await rendered()
  expect(help).toContain('请让已绑定该群的 Agent 刷新 Key')
  expect(help).not.toContain('复制查看请求')
  await state.copyInstructions(state.items[0])
  expect(clipboard).toHaveBeenCalledWith(expect.stringContaining(FOREIGN + '/api/agent-skill/lockbot-resource-agent.zip'))
  expect(clipboard.mock.calls[0][0]).not.toContain(SECRET)
})

it.each(['missing-pin', 'instance', 'foreign-path', 'secret'])('rejects unpinned or unsafe metadata: %s', async (change) => {
  const data = metadataList()
  if (change === 'missing-pin') data.service_pins[0].origin = 'https://other.example.test'
  if (change === 'instance') data.connections[0].instance_id = 'other-instance'
  if (change === 'foreign-path') data.connections[0].skill.bundle_url = FOREIGN + '/not-the-skill'
  if (change === 'secret') data.connections[0].instructions += SECRET
  fetchMock.mockResolvedValueOnce(response(metadataCaps())).mockResolvedValueOnce(response(data))
  mount()
  await flush()
  expect(state.items).toEqual([])
  expect(state.error).toBeTruthy()
  expect(clipboard).not.toHaveBeenCalled()
})
