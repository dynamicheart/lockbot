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
  const state = mount()
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
  expect(state.utilizationGroups).toEqual([{ ...utilization, nodes: ['node-a'] }])
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
  state.allocationSort = 'latest'
  const items = () => state.allocationSections[0].items
  expect(items().map((item) => item.first.allocation.expires_at)).toEqual([250, 200, 100])
  expect(items().map((item) => item.rows.length)).toEqual([1, 1, 2])
  expect(state.allocationSections[1].items).toHaveLength(1)

  state.clock = 0
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
  const booking = { kind: 'reservation', node: 'node-a', device: '0' }
  const state = await readyGroups([
    {
      ...confirmed,
      group_id: 'a',
      allocations: [
        ...confirmed.allocations,
        { kind: 'hold', node: 'node-b' },
        booking,
        { ...booking, node: 'node-b' },
      ],
    },
    { ...confirmed, group_id: 'b' },
  ])

  expect(state.detailOpen).toBe(false)
  expect(state.detailSections).toEqual([])
  const [holdings, reservations] = state.allocationSections
  const item = holdings.items.find(({ first }) => first.allocation.node === 'node-b')
  state.openWorkbenchResource(item)
  expect(state.detailSections).toHaveLength(1)
  expect(state.detailSections[0].rows.map(({ allocation }) => allocation.node)).toEqual(['node-b'])

  state.openDetails('reservations', { resource: reservations.rows[0].key })
  expect(state.detailSections).toHaveLength(1)
  expect(state.detailSections[0].rows.map(({ allocation }) => allocation.node)).toEqual(['node-a'])
  expect(state.detailUtilizationGroups).toEqual([])

  state.openDetails('group', { groupKey: holdings.rows[0].groupKey })
  expect(state.detailSections).toHaveLength(2)
  expect(
    state.detailSections.flatMap(({ rows }) => rows.map(({ group }) => group.group_id))
  ).toEqual(['a', 'a', 'a', 'a'])
  state.session = { authenticated: false, identities: [] }
  expect(state.detailOpen).toBe(false)
  expect(state.detailSections).toEqual([])
})
