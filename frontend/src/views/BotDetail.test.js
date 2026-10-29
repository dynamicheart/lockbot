import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, reactive, ssrContextKey } from 'vue'
import BotDetail from './BotDetail.vue'
import api from '../utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { validateBotState } from '../utils/stateValidation'

const model = vi.hoisted(() => ({
  route: null,
  store: null,
  requests: [],
  router: { replace: vi.fn(), push: vi.fn() },
}))
vi.mock('vue-router', () => ({ useRoute: () => model.route, useRouter: () => model.router }))
vi.mock('../stores/bots', () => ({ useBotsStore: () => model.store }))
vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({
    isAdmin: true,
    isSuperAdmin: true,
    canEditBot: (bot) => !!bot,
    canOperateBot: (bot) => !!bot,
  }),
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: { value: 'en' } }) }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))
vi.mock('../utils/stateValidation', () => ({ validateBotState: vi.fn(() => ({ warnings: [] })) }))
vi.mock('../utils/api', () => ({
  default: { get: vi.fn(async () => ({ data: {} })), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('../components/ResourceDatasourceEditor.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ResourceUtilization.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ResourceMetricsCell.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/AgentActivity.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ServiceTrafficPanel.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/ResourceStatusList.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/BotAudit.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/StatusBadge.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/LogViewer.vue', () => ({ default: { render: () => null } }))

function deferred(kind, id, options, wrap = (value) => value) {
  return new Promise((resolve, reject) => {
    const item = { kind, id: String(id), options, settled: false }
    item.resolve = (value) => {
      item.settled = true
      resolve(wrap(value))
    }
    item.reject = (error) => {
      item.settled = true
      reject(error)
    }
    model.requests.push(item)
  })
}
const latest = (kind, id) =>
  model.requests.filter((item) => item.kind === kind && item.id === String(id)).at(-1)
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
function record(id, suffix = '') {
  return {
    id: Number(id),
    name: `fixture-${id}${suffix}`,
    status: 'running',
    bot_type: 'NODE',
    platform: 'Infoflow',
    cluster_configs: '["n1"]',
    config_overrides: JSON.stringify({ LANGUAGE: id === '2' ? 'en' : 'zh' }),
    api_key: `fictional-key-${id}${suffix}`,
    webhook_url_raw: `fictional-webhook-${id}${suffix}`,
    user_id: 1,
  }
}
async function ready(id = '1', suffix = '') {
  latest('bot', id).resolve(record(id, suffix))
  latest('state', id)?.resolve({
    state: { n1: { status: 'idle', current_users: [], booking_list: [] } },
    usage: [],
  })
  latest('logs', id)?.resolve([])
  await flush()
}
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent, anchor) => {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove: (child) => {
    const list = child.parent?.children
    const index = list?.indexOf(child)
    if (index >= 0) list.splice(index, 1)
  },
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: () => {},
})
let app
function mount() {
  app = renderer.createApp(BotDetail)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.directive('loading', {})
  app.mount({ children: [] })
  return app._instance.setupState
}
beforeEach(() => {
  vi.clearAllMocks()
  model.route = reactive({ params: { id: '1' } })
  model.requests = []
  model.store = {
    getBot: vi.fn((id, options) => deferred('bot', id, options)),
    getBotState: vi.fn((id, params, options) => deferred('state', id, options)),
    getBotLogs: vi.fn((id, params, options) => deferred('logs', id, options)),
    ...Object.fromEntries(
      [
        'startBot',
        'stopBot',
        'setBotLanguage',
        'updateBot',
        'updateBotState',
        'deleteBot',
        'transferOwner',
      ].map((name) => [name, vi.fn((id) => deferred(name, id))])
    ),
  }
  api.get.mockImplementation((path, options) =>
    deferred('get', path, options, (data) => ({ data }))
  )
  api.post.mockImplementation((path, value, options) =>
    deferred('post', path, options, (data) => ({ data }))
  )
  api.delete.mockImplementation((path, options) => deferred('delete', path, options))
  ElMessageBox.confirm.mockImplementation(() => deferred('confirm', 'dialog'))
  validateBotState.mockReturnValue({ warnings: [] })
  vi.stubGlobal('document', {
    hidden: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })
  vi.stubGlobal('window', { location: { origin: 'http://example.invalid' } })
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.unstubAllGlobals()
})
it('reloads the real mounted detail component when only the route bot ID changes', async () => {
  mount()
  expect(model.store.getBot).toHaveBeenCalledTimes(1)
  model.route.params.id = '2'
  await nextTick()
  expect(model.store.getBot.mock.calls.map(([id]) => String(id))).toEqual(['1', '2'])
})

it('clears bot secrets, state and editors synchronously and fences A→B→A late reads', async () => {
  const view = mount()
  await ready('1')
  view.showWebhook = view.showAesKey = view.showToken = view.showOpApiKey = true
  view.editingState = view.viewJson = view.showTransferOwner = true
  view.stateText = 'fictional old editor'
  view.transferUsername = 'fictional old recipient'
  const oldBot = view.refreshBot(),
    oldState = view.fetchState(),
    oldLogs = view.fetchCommandLogs()
  const old = { bot: latest('bot', '1'), state: latest('state', '1'), logs: latest('logs', '1') }
  model.route.params.id = '2'
  expect(view.bot).toBe(null)
  expect(view.stateData).toBe(null)
  expect(view.stateUsage).toEqual([])
  expect(view.stateText).toBe('')
  expect(view.showWebhook || view.showAesKey || view.showToken || view.showOpApiKey).toBe(false)
  expect(view.editingState || view.viewJson || view.showTransferOwner).toBe(false)
  expect(view.transferUsername).toBe('')
  const middle = { bot: latest('bot', '2'), state: latest('state', '2'), logs: latest('logs', '2') }
  model.route.params.id = '1'
  await ready('1', '-new')
  old.bot.resolve(record('1', '-old'))
  old.state.resolve({ state: { old: true }, usage: [{ purpose: 'fictional old' }] })
  old.logs.resolve([{ message: '[old] query' }])
  middle.bot.resolve(record('2'))
  middle.state.resolve({ state: { wrong: true }, usage: [] })
  middle.logs.resolve([{ message: '[wrong] query' }])
  await Promise.all([oldBot, oldState, oldLogs])
  await flush()
  expect(view.bot.name).toBe('fixture-1-new')
  expect(view.bot.api_key).toBe('fictional-key-1-new')
  expect(view.stateData).toHaveProperty('n1')
  expect(view.stateUsage).toEqual([])
  expect(view.commandLogs).toEqual([])
  expect(
    old.bot.options.signal.aborted &&
      old.state.options.signal.aborted &&
      old.logs.options.signal.aborted
  ).toBe(true)
})

it('ignores old 404 redirects and errors, and keeps a failed current detail empty', async () => {
  const view = mount()
  const old = latest('bot', '1')
  model.route.params.id = '2'
  const state = latest('state', '2')
  old.reject({ response: { status: 404 } })
  await flush()
  expect(model.router.replace).not.toHaveBeenCalled()
  expect(view.loading).toBe(true)
  latest('bot', '2').reject({ response: { status: 500 } })
  await flush()
  state.resolve({ state: { wrong: true }, usage: [{ purpose: 'fictional secret' }] })
  await flush()
  expect(view.bot).toBe(null)
  expect(view.stateData).toBe(null)
  expect(view.stateUsage).toEqual([])
  expect(view.loading).toBe(false)
})

it('keeps only the latest read in a channel and prevents old failures clearing newer state', async () => {
  const view = mount()
  await ready()
  const first = view.fetchState(),
    old = latest('state', '1'),
    second = view.fetchState(),
    current = latest('state', '1')
  current.resolve({ state: { fresh: true }, usage: [] })
  await second
  old.reject(new Error('old request'))
  await first
  expect(view.stateData).toEqual({ fresh: true })
  expect(old.options.signal.aborted).toBe(true)
})

it.each([
  ['handleStart', 'startBot'],
  ['handleStop', 'stopBot'],
  ['handleLanguageChange', 'setBotLanguage'],
  ['handleGenerateApiKey', 'post'],
  ['handleRevokeApiKey', 'delete'],
  ['datasource', 'updateBot'],
])('pins %s to its original bot and suppresses its late result', async (handler, kind) => {
  const view = mount()
  await ready()
  const operation =
    handler === 'datasource'
      ? view.datasourceActions.save({ name: 'fictional-source' })
      : view[handler]('en')
  const pending = latest(kind, kind === 'post' || kind === 'delete' ? '/bots/1/api-key' : '1')
  expect(pending).toBeDefined()
  model.route.params.id = '2'
  await ready('2')
  pending.resolve(kind === 'post' ? { api_key: 'fictional-old-key' } : record('1', '-mutation'))
  if (handler === 'datasource')
    await expect(operation).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  else await operation
  await flush()
  expect(view.bot.id).toBe(2)
  expect(view.bot.api_key).toBe('fictional-key-2')
  expect(view.botLanguage).toBe('en')
  expect(model.store.getBot.mock.calls.filter(([id]) => String(id) === '2')).toHaveLength(1)
  expect(ElMessage.success).not.toHaveBeenCalled()
})

it.each(['handleDelete', 'handleTransferOwner', 'saveState'])(
  'does not submit %s after confirmation navigates away',
  async (handler) => {
    const view = mount()
    await ready()
    view.transferUsername = 'fictional recipient'
    view.stateText = '{}'
    if (handler === 'saveState')
      validateBotState.mockReturnValue({ warnings: ['fictional warning'] })
    const operation = view[handler]()
    const dialog = latest('confirm', 'dialog')
    expect(dialog).toBeDefined()
    model.route.params.id = '2'
    await ready('2')
    dialog.resolve()
    await operation
    expect(model.store.deleteBot).not.toHaveBeenCalled()
    expect(model.store.transferOwner).not.toHaveBeenCalled()
    expect(model.store.updateBotState).not.toHaveBeenCalled()
  }
)

it('invalidates captured data-source and metrics callbacks after navigation', async () => {
  const view = mount()
  await ready()
  const actions = view.datasourceActions,
    receive = view.receiveMetrics
  const access = actions.authorize({ name: 'fictional source' })
  const pending = latest('post', '/bots/1/utilization-access')
  model.route.params.id = '2'
  await ready('2')
  pending.resolve({ token: 'fictional-old-grant' })
  await expect(access).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  await expect(actions.save({})).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  await expect(actions.authorize({})).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  receive({ enabled: true, old: true })
  expect(view.resourceMetrics).toEqual({ enabled: false })
  expect(model.store.updateBot).not.toHaveBeenCalled()
  expect(pending.options.signal.aborted).toBe(true)
})

it('aborts reads and blocks deferred confirmations and redirects after unmount', async () => {
  const view = mount()
  await ready()
  const operation = view.handleDelete()
  const dialog = latest('confirm', 'dialog')
  const read = view.refreshBot()
  const pending = latest('bot', '1')
  app.unmount()
  app = null
  expect(pending.options.signal.aborted).toBe(true)
  pending.reject({ response: { status: 404 } })
  dialog.resolve()
  await Promise.all([read, operation])
  expect(model.store.deleteBot).not.toHaveBeenCalled()
  expect(model.router.replace).not.toHaveBeenCalled()
  expect(model.router.push).not.toHaveBeenCalled()
})

it('does not accept an old view link, summary or generated key when the same ID is revisited', async () => {
  const view = mount()
  await ready()
  const link = view.refreshViewLink(),
    oldLink = latest('bot', '1')
  const summary = view.loadAgentSummary(),
    oldSummary = latest('get', '/bots/1/agent-summary')
  const key = view.handleGenerateApiKey(),
    oldKey = latest('post', '/bots/1/api-key')
  model.route.params.id = '2'
  await ready('2')
  model.route.params.id = '1'
  await ready('1', '-revisited')
  oldLink.resolve({ view_url: 'fictional-old-view', view_expires_at: 'old' })
  oldSummary.resolve({ authorizations: 999 })
  oldKey.resolve({ api_key: 'fictional-old-key' })
  await Promise.all([link, summary, key])
  await flush()
  expect(view.bot.name).toBe('fixture-1-revisited')
  expect(view.bot.view_url).not.toBe('fictional-old-view')
  expect(view.bot.api_key).toBe('fictional-key-1-revisited')
  expect(view.agentSummary).toBe(null)
})

it.each([
  ['handleDelete', 'deleteBot'],
  ['handleTransferOwner', 'transferOwner'],
  ['saveState', 'updateBotState'],
])('does not let an already submitted %s refresh or clear another bot', async (handler, kind) => {
  const view = mount()
  await ready()
  view.transferUsername = 'fictional old recipient'
  view.stateText = '{}'
  const operation = view[handler]()
  const dialog = latest('confirm', 'dialog')
  dialog?.resolve()
  await flush()
  const pending = latest(kind, '1')
  expect(pending).toBeDefined()
  model.route.params.id = '2'
  await ready('2')
  view.showTransferOwner = true
  view.transferUsername = 'fictional new recipient'
  view.editingState = true
  pending.resolve({ warnings: [] })
  await operation
  await flush()
  expect(view.bot.id).toBe(2)
  expect(view.transferUsername).toBe('fictional new recipient')
  expect(view.showTransferOwner).toBe(true)
  expect(view.editingState).toBe(true)
  expect(model.store.getBot.mock.calls.filter(([id]) => String(id) === '2')).toHaveLength(1)
  expect(model.router.push).not.toHaveBeenCalled()
})

it('limits detailed-note consent to Agent operation history and never changes state reads', async () => {
  const view = mount()
  await ready('1')
  view.activityTab = 'agents'
  view.agentTab = 'history'
  view.showPrivateTasks = true
  expect(view.privateTaskView).toBe(true)
  const pending = view.fetchState()
  expect(model.store.getBotState.mock.calls.at(-1)[1].include_private).toBe(false)
  latest('state', '1').resolve({ state: {}, usage: [] })
  await pending
  view.agentTab = 'access'
  expect(view.showPrivateTasks).toBe(false)
  expect(view.privateTaskView).toBe(false)
  view.agentTab = 'history'
  expect(view.privateTaskView).toBe(false)
})

it('clears detailed-note consent when leaving Agent history or navigating bots', async () => {
  const view = mount()
  await ready('1')
  view.activityTab = 'agents'
  view.agentTab = 'history'
  view.showPrivateTasks = true
  view.activityTab = 'logs'
  expect(view.showPrivateTasks).toBe(false)
  view.activityTab = 'agents'
  view.agentTab = 'history'
  view.showPrivateTasks = true
  model.route.params.id = '2'
  expect(view.showPrivateTasks).toBe(false)
  expect(view.privateTaskView).toBe(false)
  expect(model.store.getBotState.mock.calls.at(-1)[1].include_private).toBe(false)
  expect(view.stateUsage).toEqual([])
})
