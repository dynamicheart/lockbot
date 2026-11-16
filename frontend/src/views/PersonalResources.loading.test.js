import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { createI18n } from 'vue-i18n'
import zhCN from '../i18n/zh-CN'
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
let router
let initialPath
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
afterEach(() => {
  app?.unmount()
  initialPath = undefined
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => data })
function mount() {
  app = renderer.createApp({ ...PersonalResources, render: () => null })
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/me/:view?/:selection?', component: { render: () => null } }],
  })
  app.use(router)
  app.use(createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zhCN } }))
  app.provide(ssrContextKey, {})
  if (initialPath) return router.replace(initialPath).then(mountView)
  return mountView()
}
function mountView() {
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

it('summarizes live card samples for a whole node and only held cards for a card group', async () => {
  initialPath = '/me/holdings'
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) =>
      url.endsWith('/session')
        ? response({ authenticated: true, identities: [] })
        : response({ groups: [] })
    )
  )
  const state = await mount()
  await flush()
  const expires_at = new Date(Date.now() + 60_000).toISOString()
  const configuration = {
    staleSeconds: 90,
    nodeMapping: { whole: 'whole', partial: 'partial' },
    deviceMapping: { 'partial:dev0': '0', 'partial:dev1': '1' },
  }
  state.groups = [
    {
      label: 'Test',
      status: 'ok',
      utilization: { connection: 'metric-source', configuration },
      allocations: [
        { kind: 'hold', node: 'whole', expires_at },
        { kind: 'hold', node: 'partial', device: '0', expires_at },
        { kind: 'hold', node: 'partial', device: '1', expires_at },
      ],
    },
  ]
  const card = (node, device, value, observedAt = Date.now()) => ({
    node,
    device,
    metrics: { compute: { value, observedAt } },
  })
  state.utilizationSnapshots['metric-source'] = {
    enabled: true,
    config: configuration,
    snapshot: {
      cards: [
        card('whole', '0', 20),
        card('whole', '1', 60),
        card('partial', '0', 0),
        card('partial', '1', 20),
        card('partial', '2', 100),
      ],
    },
  }
  expect(state.detailUtilizationGroups[0].nodes).toEqual(['partial', 'whole'])
  const items = state.detailSections[0].items
  const key = (node) => items.find(({ first }) => first.allocation.node === node).key
  expect(state.cardUtilization[key('whole')]).toBe(40)
  expect(state.cardUtilization[key('partial')]).toBe(10)
  state.utilizationSnapshots['metric-source'].snapshot.cards[3].metrics.compute.observedAt -=
    120_000
  expect(state.cardUtilization[key('partial')]).toBeUndefined()
  state.toggleNode(key('whole'))
  expect(state.cardUtilization[key('whole')]).toBeUndefined()
  const snapshot = state.utilizationSnapshots['metric-source']
  const { holdingsKey } = state.utilizationGroups[0]
  state.updateUtilizationSnapshot('metric-source', holdingsKey, snapshot)
  expect(state.utilizationSnapshots['metric-source']).toBeDefined()
  state.groups[0].allocations.pop()
  expect(state.utilizationGroups[0].nodes).toEqual(['partial', 'whole'])
  expect(state.utilizationSnapshots['metric-source']).toBeUndefined()
  state.updateUtilizationSnapshot('metric-source', holdingsKey, snapshot)
  expect(state.utilizationSnapshots['metric-source']).toBeUndefined()
})

it('rejects an older backend grant even when its datasource matches', async () => {
  const state = await readyGroups([confirmed])
  const configuration = {
    endpoint: 'https://metrics.example.invalid',
    accessMode: 'short_lived',
    profile: 'lockbot-v1',
    nodeMapping: { n1: 'collector-node' },
  }
  state.groups = [
    {
      ...confirmed,
      allocations: [{ kind: 'hold', node: 'n1' }],
      utilization: { connection: 'source', configuration },
    },
  ]
  const access = { scope: 'metrics:read', token: 'synthetic-metrics-access' }
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ configuration, access }))
  )
  await expect(state.authorizeUtilization('source', configuration, {})).rejects.toMatchObject({
    code: 'grant',
  })
  const restricted = { ...access, scope_version: 2, node_ids: ['n1'] }
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ configuration, access: restricted }))
  )
  await expect(state.authorizeUtilization('source', configuration, {})).resolves.toEqual(restricted)
})

it('tracks independently changed bot scopes within the same verified principal', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ authenticated: false, identities: [] }))
  )
  const state = mount()
  await flush()
  const principal = { issuer: 'fixture', platform: 'infoflow', realm: 'attested', subject: 'alice' }
  expect(state.identityScope([{ ...principal, scopes: ['bot-a'] }])).not.toBe(
    state.identityScope([{ ...principal, scopes: ['bot-a', 'bot-b'] }])
  )
  expect(state.identityScope([{ ...principal, scopes: ['bot-a', 'bot-b'] }])).toBe(
    state.identityScope([{ ...principal, scopes: ['bot-b', 'bot-a'] }])
  )
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
  const state = await mount()
  await flush()
  await vi.waitFor(() => expect(state.resourcesReady).toBe(true))
  return state
}

it('polls mapped holdings and drops the snapshot when the holding or scope disappears', async () => {
  const utilization = {
    connection: 'binding-a',
    configuration: { nodeMapping: { 'node-a': 'host-a', 'node-b': 'host-b' } },
  }
  const group = {
    ...confirmed,
    utilization,
    allocations: [
      ...confirmed.allocations,
      { kind: 'hold', node: 'unmapped' },
      { kind: 'reservation', node: 'node-b' },
    ],
  }
  const state = await readyGroups([group])
  expect(state.utilizationGroups).toMatchObject([{ ...utilization, nodes: ['node-a'] }])
  state.utilizationSnapshots['binding-a'] = { enabled: true, snapshot: {} }
  state.groups = [{ ...group, status: 'unknown' }]
  expect(state.utilizationGroups).toEqual([])
  expect(state.utilizationSnapshots).toEqual({})
})

it('shows remaining lease time across UTC offsets and keeps queued records without a deadline distinct', async () => {
  const state = await readyGroups([
    {
      ...confirmed,
      allocations: [...confirmed.allocations, { kind: 'reservation', node: 'node-b' }],
    },
  ])
  state.clock = Date.parse('2026-11-14T12:00:00Z')
  const expiry = '2026-11-14T21:15:00+08:00'
  expect(state.remaining(expiry)).toBe('1 小时 15 分钟')
  expect(state.remaining('2026-11-14 13:15:00')).toBe('1 小时 15 分钟')
  expect(state.remaining(null)).toBe('未提供')
  state.clock = Date.parse(expiry) - 1000
  expect(state.remaining(expiry)).toBe('不到 1 分钟')
  state.clock += 1000
  expect(state.remaining(expiry)).toBe('已到期')
  expect(state.allocationSections.map((section) => section.timeLabel)).toEqual(['剩余时间', ''])
})

it.each([
  [{ host: '2001:db8::1', port: 2222 }, '[2001:db8::1]:2222'],
  [
    { host: 'compute.example.test', ip: '192.0.2.42', port: 2222 },
    'compute.example.test:2222 · 192.0.2.42:2222',
  ],
])(
  'shows one node address entry for held cards until expiration (%j)',
  async (address, expected) => {
    const now = Date.parse('2026-11-14T12:00:00Z')
    const state = await readyGroups([
      {
        ...confirmed,
        allocations: [0, 60, 120].map((seconds, device) => ({
          kind: 'hold',
          node: 'node-a',
          device: String(device),
          expires_at: new Date(now + seconds * 1000).toISOString(),
          node_access: device === 1 ? undefined : address,
        })),
      },
    ])
    state.clock = now
    const [item] = state.allocationSections[0].items
    expect(state.holdingAddress(item)).toBe(expected)
    state.clock = now + 120_000
    expect(state.holdingAddress(item)).toBe('')
  }
)

it('reads guidance on demand and clears it when the holding expires', async () => {
  const now = Date.now()
  const expiresAt = now + 60_000
  const descriptor = {
    protocol: 'lockbot.resource-guidance.v1',
    id: 'a'.repeat(32),
    revision: 'r1',
    title: 'Device guide',
  }
  const state = await readyGroups([
    {
      ...confirmed,
      allocations: [
        {
          kind: 'hold',
          node: 'node-a',
          device: '0',
          expires_at: new Date(expiresAt).toISOString(),
          resource_guidance: [descriptor],
        },
      ],
    },
  ])
  state.clock = now
  const [item] = state.allocationSections[0].items
  const [reference] = state.holdingGuidance(item)
  expect(reference).toEqual({ ...descriptor, expiresAt })
  const fetch = vi.fn(async () =>
    response({ ...descriptor, summary: 'Fixture text', documentation_url: 'javascript:alert(1)' })
  )
  vi.stubGlobal('fetch', fetch)
  await state.openGuidance(reference)
  expect(fetch.mock.calls[0][0]).toBe('/api/personal-panel/resource-guidance/' + descriptor.id)
  expect(state.guidanceDialog).toMatchObject({ summary: 'Fixture text', url: null })
  state.clock = expiresAt
  await flush()
  expect(state.guidanceDialog).toBe(null)
  expect(state.holdingGuidance(item)).toEqual([])
})

it('filters holdings and bookings across groups without changing summary counts', async () => {
  const state = await readyGroups([
    {
      ...confirmed,
      group_id: 'a',
      label: 'Group A',
      allocations: [
        { kind: 'hold', node: 'node-10', device: '0', expires_at: 100 },
        {
          kind: 'hold',
          node: 'node-2',
          device: '1',
          expires_at: 200,
          own_task: { category: '模型测试' },
        },
        { kind: 'hold', node: 'node-3' },
      ],
    },
    {
      ...confirmed,
      group_id: 'b',
      label: 'Group B',
      allocations: [{ kind: 'reservation', node: 'node-2', device: '1' }],
    },
  ])
  state.clock = 0
  const heldNodes = () => state.allocationSections[0].rows.map(({ allocation }) => allocation.node)
  expect(heldNodes()).toEqual(['node-10', 'node-2', 'node-3'])
  state.allocationSort = 'latest'
  expect(heldNodes()).toEqual(['node-2', 'node-10', 'node-3'])
  state.allocationSort = 'resource'
  expect(heldNodes()).toEqual(['node-2', 'node-3', 'node-10'])
  state.allocationQuery = 'NODE-2 dev1'
  expect(heldNodes()).toEqual(['node-2'])
  expect(state.allocationSections[1].rows).toHaveLength(1)
  state.allocationQuery = '模型测试'
  expect(heldNodes()).toEqual(['node-2'])
  expect(state.allocationSections[1].rows).toHaveLength(0)
  state.allocationGroup = state.allocationGroups[1].value
  expect(heldNodes()).toEqual([])
  expect(state.statistics.map(({ value }) => value)).toEqual([3, 2, 1])
})

it('groups held cards within one resource group and sorts by the earliest card deadline', async () => {
  const card = (node, device, expires_at) => ({ kind: 'hold', node, device, expires_at })
  const state = await readyGroups([
    {
      ...confirmed,
      group_id: 'a',
      allocations: [
        card('node-a', '0', 300),
        card('node-a', '1', 100),
        card('node-b', '0', 200),
        { ...card('node-a', '0', 400), kind: 'reservation' },
      ],
    },
    { ...confirmed, group_id: 'b', allocations: [card('node-a', '0', 250)] },
  ])
  state.clock = 0
  state.allocationSort = 'latest'
  const items = () => state.allocationSections[0].items
  expect(items().map((item) => item.first.allocation.expires_at)).toEqual([250, 200, 100])
  expect(items().map((item) => item.rows.length)).toEqual([1, 1, 2])
  expect(state.allocationSections[1].items).toHaveLength(1)

  expect(state.nodeRemaining(items()[2])).toBe('1 分钟 至 5 分钟')
  expect(state.nodeRemaining({ deadlines: [300_000] })).toBe('5 分钟')
  expect(state.nodeRemaining({ deadlines: [300_000, NaN] })).toBe('5 分钟（部分未知）')
  const { key } = items()[2]
  expect(state.expandedNodes.has(key)).toBe(false)
  state.toggleNode(key)
  expect(state.expandedNodes.has(key)).toBe(true)
  state.allocationQuery = 'dev1'
  expect(items().flatMap((item) => item.rows.map((row) => row.allocation.device))).toEqual(['1'])
})

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
  expect(state.groupCounts(group)).toEqual([])
  expect(state.groupCounts({ ...confirmed, capacity: { nodes: 4, cards: 8 } })).toEqual([
    { value: 1, total: 4, label: '机器（我的 / 全群）' },
    { value: 1, total: 8, label: '卡（我的 / 全群）' },
    { value: 0, label: '我的预约' },
  ])
  expect(state.groupCounts(confirmed).map((item) => item.total ?? null)).toEqual([null, null, null])
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

it('clears the browser authorization only after confirmation', async () => {
  const state = await readyGroups([confirmed])
  const fetch = vi.fn(async () => response({}))
  vi.stubGlobal('fetch', fetch)
  const confirm = vi.spyOn(ElMessageBox, 'confirm')

  for (const action of ['cancel', 'close']) {
    confirm.mockRejectedValueOnce(action)
    await state.run(state.confirmLogout)
    expect(fetch).not.toHaveBeenCalled()
    expect(state.session.authenticated).toBe(true)
    expect(state.error).toBe('')
  }

  confirm.mockResolvedValueOnce('confirm')
  await state.run(state.confirmLogout)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toMatch(/\/logout$/)
  expect(state.session.authenticated).toBe(false)
})

it('counts confirmed batches while other sources are still loading', async () => {
  let feed
  const body = new ReadableStream({
    start(controller) {
      feed = controller
    },
  })
  const send = (event) => feed.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n'))
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) =>
      url.endsWith('/session')
        ? response({ authenticated: true, identities: [identity] })
        : new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } })
    )
  )
  const state = mount()
  send({ event: 'start', total: 2 })
  await vi.waitFor(() => expect(state.reportProgress).toEqual({ done: 0, total: 2 }))
  expect(state.statistics.map((item) => item.value)).toEqual([null, null, null])

  send({ event: 'batch', index: 1, groups: [confirmed] })
  await vi.waitFor(() => expect(state.statistics.map((item) => item.value)).toEqual([1, 1, 0]))
  expect(state.resourceLoading).toBe(true)
  expect(state.resourcesReady).toBe(false)

  send({ event: 'batch', index: 0, groups: [{ ...confirmed, status: 'unknown', allocations: [] }] })
  send({ event: 'complete' })
  feed.close()
  await vi.waitFor(() => expect(state.resourcesReady).toBe(true))
  expect(state.reportProgress).toEqual({ done: 2, total: 2 })
  expect(state.statistics.map((item) => item.value)).toEqual([1, 1, 0])
})

it('removes a partial report when the stream invalidates authorization', async () => {
  let feed
  const body = new ReadableStream({
    start(controller) {
      feed = controller
    },
  })
  const send = (event) => feed.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n'))
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) =>
      url.endsWith('/session')
        ? response({ authenticated: true, identities: [identity] })
        : new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } })
    )
  )
  const state = mount()
  send({ event: 'start', total: 2 })
  send({ event: 'batch', index: 0, groups: [confirmed] })
  await vi.waitFor(() => expect(state.groups).toHaveLength(1))

  send({ event: 'unavailable' })
  feed.close()
  await vi.waitFor(() => expect(state.resourceLoading).toBe(false))
  expect(state.groups).toEqual([])
  expect(state.statistics.every((item) => item.value === null)).toBe(true)
})

it('opens only the selected resource or group and closes on a change of identity scope', async () => {
  const booking = { kind: 'reservation', node: 'node-a', device: '0', detail_id: 'booking-a' }
  const state = await readyGroups([
    {
      ...confirmed,
      group_id: 'a',
      detail_id: 'group-a',
      allocations: [
        ...confirmed.allocations,
        { kind: 'hold', node: 'node-b', detail_id: 'holding-b' },
        booking,
        { ...booking, node: 'node-b', detail_id: 'booking-b' },
      ],
    },
    { ...confirmed, group_id: 'b' },
  ])

  expect(state.detailPage).toBeNull()
  expect(state.detailSections).toEqual([])
  const [holdings, reservations] = state.allocationSections
  const item = holdings.items.find(({ first }) => first.allocation.node === 'node-b')
  await state.openWorkbenchResource(item)
  expect(router.currentRoute.value.fullPath).toBe('/me/holdings?node=holding-b')
  expect(state.detailSections).toHaveLength(1)
  expect(state.expandedNodes.has(item.key)).toBe(true)
  await router.push('/me')
  await router.push('/me/holdings/holding-b')
  expect(state.detailSections[0].rows.map(({ allocation }) => allocation.node)).toEqual(['node-b'])

  await state.openBooking(reservations.rows[0])
  expect(state.detailSections).toHaveLength(1)
  expect(state.detailSections[0].rows.map(({ allocation }) => allocation.node)).toEqual(['node-a'])
  expect(state.detailUtilizationGroups).toEqual([])

  await state.openGroup(holdings.rows[0].group)
  expect(state.detailSections).toHaveLength(2)
  expect(
    state.detailSections.flatMap(({ rows }) => rows.map(({ group }) => group.group_id))
  ).toEqual(['a', 'a', 'a', 'a'])
  await router.push('/me/holdings/unknown')
  expect(state.detailSections).toEqual([])
  await router.push('/me/group/group-a')
  expect(state.detailSections).toHaveLength(2)
  state.session = { authenticated: false, identities: [] }
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/me/groups'))
  expect(state.detailSections).toEqual([])
})

it('restores a card-level node view from its direct URL as reports arrive', async () => {
  initialPath = '/me/holdings?node=node-a-details'
  const state = await readyGroups([
    {
      ...confirmed,
      detail_id: 'group-a',
      allocations: ['0', '1'].map((device) => ({
        kind: 'hold',
        node: 'node-a',
        device,
        detail_id: 'card-' + device,
        node_detail_id: 'node-a-details',
      })),
    },
  ])
  expect(router.currentRoute.value.fullPath).toBe(initialPath)
  expect(state.detailSections[0].rows).toHaveLength(2)
  await vi.waitFor(() =>
    expect(state.expandedNodes.has(state.detailSections[0].items[0].key)).toBe(true)
  )
})

it('keeps a node on the overview when another card on that node has already expired', async () => {
  const now = Date.parse('2026-11-14T12:00:00Z')
  const state = await readyGroups([
    {
      ...confirmed,
      allocations: [-1, 5, 20, 40].map((minutes, device) => ({
        kind: 'hold',
        node: 'node-a',
        device,
        expires_at: new Date(now + minutes * 60_000).toISOString(),
        node_detail_id: 'node-a-details',
      })),
    },
  ])
  state.clock = now
  expect(state.expiringItems).toHaveLength(1)
  expect(state.expiringItems[0].endingCards).toBe(2)
  expect(state.expiringItems[0].first.allocation.device).toBe(1)
  expect(state.holdingPath(state.expiringItems[0])).toEqual({
    path: '/me/holdings',
    query: { node: 'node-a-details' },
  })
})

it('drops expired holds from totals, group details and metrics while retaining unknown deadlines', async () => {
  const now = Date.parse('2026-11-14T12:00:00Z')
  const group = {
    ...confirmed,
    capacity: { nodes: 5, cards: 16 },
    utilization: {
      connection: 'binding-a',
      configuration: { nodeMapping: { 'node-a': 'host-a', 'node-b': 'host-b' } },
    },
    allocations: [
      { kind: 'hold', node: 'node-a', device: '0', expires_at: (now + 60_000) / 1000 },
      { kind: 'hold', node: 'node-a', device: '1', expires_at: (now + 120_000) / 1000 },
      { kind: 'hold', node: 'node-b', expires_at: null },
      { kind: 'reservation', node: 'node-a' },
    ],
  }
  const state = await readyGroups([group])
  state.clock = now
  const summary = () => state.statistics.map(({ value }) => value)
  expect(summary()).toEqual([2, 2, 1])
  const { holdingsKey } = state.utilizationGroups[0]
  state.updateUtilizationSnapshot('binding-a', holdingsKey, { enabled: true })
  state.clock = now + 60_000
  expect(summary()).toEqual([2, 1, 1])
  expect(state.utilizationSnapshots['binding-a']).toBeUndefined()
  state.updateUtilizationSnapshot('binding-a', holdingsKey, { enabled: true })
  expect(state.utilizationSnapshots['binding-a']).toBeUndefined()
  state.clock = now + 120_000
  expect(summary()).toEqual([1, 0, 1])
  expect(state.groupCounts(group).map(({ value }) => value)).toEqual([1, 0, 1])
  expect(state.allocationSections[0].rows.map(({ allocation }) => allocation.node)).toEqual([
    'node-b',
  ])
  expect(state.utilizationGroups[0].nodes).toEqual(['node-b'])
})
