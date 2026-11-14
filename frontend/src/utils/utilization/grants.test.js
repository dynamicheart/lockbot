import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createGrantedMetricsCollector, createViewMetricsAuthorizer, linkSignals } from './grants'
import { createMetricsPoller } from './poller'
import { ProviderError, validateConfig } from './prometheus'
const source = () =>
  validateConfig({
    id: 'demo',
    profile: 'lockbot-v1',
    accessMode: 'short_lived',
    endpoint: 'https://metrics.example.invalid',
    nodeMapping: { node1: 'host1' },
  })
const grant = (token = 'metrics-only') => ({
  token,
  scope: 'metrics:read',
  expires_at: Date.now() / 1000 + 300,
})
const deferred = () => {
  let resolve, reject
  const promise = new Promise((ok, no) => {
    resolve = ok
    reject = no
  })
  return { promise, resolve, reject }
}
const signal = () => new AbortController().signal
function collector(overrides = {}) {
  const authorize = vi.fn(async () => grant()),
    query = vi.fn(async () => ({ cards: [] }))
  return {
    authorize,
    query,
    ...createGrantedMetricsCollector({
      getConfig: source,
      getNodes: () => ['node1'],
      getBearer: () => '',
      authorize,
      query,
      ...overrides,
    }),
  }
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-14T00:00:00Z'))
})
afterEach(() => vi.useRealTimers())
it.each([null, '2026-12-14T00:00:00Z'])(
  'renews across multiple five-minute expiries independently of view lifetime %s',
  async (viewExpiry) => {
    const expiry = new Map()
    let serial = 0
    const authorize = vi.fn(
      createViewMetricsAuthorizer({
        getScope: () => ({ botId: '7', generation: 1, token: 'stable-view-code' }),
        fetchImpl: async () => {
          const value = grant(`metrics-${++serial}`)
          expiry.set(value.token, value.expires_at)
          return response({
            health: 'running',
            utilization_config: source(),
            utilization_access: value,
            view_expires_at: viewExpiry,
          })
        },
      })
    )
    const query = vi.fn(async (_config, _nodes, { bearer }) => {
      expect(expiry.get(bearer) * 1000).toBeGreaterThan(Date.now())
      return {}
    })
    const item = collector({ authorize, query })
    for (let i = 0; i <= 30; i++) {
      await item.collect(signal())
      await vi.advanceTimersByTimeAsync(30000)
    }
    expect(authorize).toHaveBeenCalledTimes(4)
    expect(query).toHaveBeenCalledTimes(31)
  }
)
it('forces only one grant refresh and one query retry after source authentication rejection', async () => {
  const item = collector()
  item.query.mockRejectedValueOnce(new ProviderError('auth'))
  await item.collect(signal())
  expect(item.authorize).toHaveBeenCalledTimes(2)
  expect(item.authorize.mock.calls[1][1].force).toBe(true)
  expect(item.query).toHaveBeenCalledTimes(2)
  item.query.mockRejectedValue(new ProviderError('auth'))
  await expect(item.collect(signal())).rejects.toMatchObject({ code: 'auth' })
  expect(item.query).toHaveBeenCalledTimes(4)
  expect(item.authorize).toHaveBeenCalledTimes(3)
})
it.each([
  ['limited', 90],
  ['auth', 60],
])('never bypasses source %s Retry-After', async (code, seconds) => {
  const item = collector()
  item.query.mockRejectedValue(new ProviderError(code, seconds))
  await expect(item.collect(signal())).rejects.toMatchObject({ code, retryAfter: seconds })
  expect(item.query).toHaveBeenCalledTimes(1)
  expect(item.authorize).toHaveBeenCalledTimes(1)
})
it('preserves authorization rate limits without a datasource request', async () => {
  const item = collector()
  item.authorize.mockRejectedValue({ response: { status: 429, headers: { 'retry-after': '90' } } })
  await expect(item.collect(signal())).rejects.toMatchObject({ code: 'limited', retryAfter: 90 })
  expect(item.query).not.toHaveBeenCalled()
})
it('rejects missing scope, expired grants and personal credential-shaped replies before querying', async () => {
  for (const value of [
    { token: 'lbr_personal', expires_at: Date.now() / 1000 + 300 },
    { ...grant(), expires_at: 0 },
    null,
  ]) {
    const item = collector()
    item.authorize.mockResolvedValue(value)
    await expect(item.collect(signal())).rejects.toMatchObject({ code: 'grant' })
    expect(item.query).not.toHaveBeenCalled()
  }
})
it('aborts even a cancellation-ignorant authorizer and discards its late grant after reset', async () => {
  const waiting = deferred(),
    item = collector()
  item.authorize.mockReturnValueOnce(waiting.promise)
  const abort = new AbortController()
  const first = item.collect(abort.signal)
  abort.abort()
  await expect(first).rejects.toMatchObject({ code: 'timeout' })
  item.reset()
  waiting.resolve(grant('old-metrics'))
  await Promise.resolve()
  await item.collect(signal())
  expect(item.query).toHaveBeenCalledTimes(1)
  expect(item.query.mock.calls[0][2].bearer).not.toBe('old-metrics')
})
it('invalidates in-flight and cached grants when source or resource scope changes', async () => {
  let config = source(),
    nodes = ['node1']
  const waiting = deferred()
  const item = collector({ getConfig: () => config, getNodes: () => nodes })
  item.authorize.mockReturnValueOnce(waiting.promise)
  const old = item.collect(signal())
  config = { ...source(), endpoint: 'https://other.example.invalid' }
  nodes = ['node2']
  await item.collect(signal())
  waiting.resolve(grant('wrong-source'))
  await expect(old).rejects.toMatchObject({ code: 'grant' })
  expect(item.query).toHaveBeenCalledTimes(1)
  expect(item.query.mock.calls[0][0].endpoint).toContain('other')
})
it('keeps independent credentials and query behavior without grant renewal', async () => {
  const item = collector({
    getConfig: () => ({ ...source(), accessMode: 'independent' }),
    getBearer: () => 'independent-read-only',
  })
  await item.collect(signal())
  expect(item.authorize).not.toHaveBeenCalled()
  expect(item.query.mock.calls[0][2].bearer).toBe('independent-read-only')
})
it('uses one fifteen-second deadline for authorization plus a query', async () => {
  const item = collector()
  item.authorize.mockImplementation(
    () => new Promise((resolve) => setTimeout(() => resolve(grant()), 13000))
  )
  item.query.mockImplementation(
    (_config, _nodes, { signal }) =>
      new Promise((_ok, reject) =>
        signal.addEventListener('abort', () => reject(new ProviderError('timeout')))
      )
  )
  const publish = vi.fn(),
    poll = createMetricsPoller({ collect: item.collect, publish, now: Date.now })
  poll.start()
  await vi.advanceTimersByTimeAsync(13000)
  expect(item.query).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(2000)
  expect(publish).toHaveBeenCalledWith({ loading: false, error: 'timeout' })
  poll.stop()
})
it('links collection deadlines and management navigation cancellation', () => {
  const deadline = new AbortController(),
    route = new AbortController(),
    linked = linkSignals(deadline.signal, route.signal)
  deadline.abort()
  expect(linked.signal.aborted).toBe(true)
  linked.dispose()
  const next = linkSignals(signal(), route.signal)
  route.abort()
  expect(next.signal.aborted).toBe(true)
  next.dispose()
})
function response(data, status = 200, headers = {}) {
  return { status, ok: status === 200, headers: new Headers(headers), json: async () => data }
}
it('mints using the same origin/view scope and sends only the returned metrics grant to the datasource', async () => {
  const scope = {
    botId: '7',
    generation: 1,
    token: 'shared-view-code',
    source: source(),
    access: grant('cached-metrics'),
  }
  const fetchImpl = vi.fn(async () =>
    response({
      health: 'running',
      utilization_config: source(),
      utilization_access: grant('new-metrics'),
    })
  )
  const authorize = createViewMetricsAuthorizer({ getScope: () => scope, fetchImpl })
  expect((await authorize(source(), { signal: signal() })).token).toBe('cached-metrics')
  expect(fetchImpl).not.toHaveBeenCalled()
  const value = await authorize(source(), { signal: signal(), force: true })
  expect(value.token).toBe('new-metrics')
  expect(fetchImpl.mock.calls[0][0]).toBe('/api/bot-views/7/state')
  expect(fetchImpl.mock.calls[0][1]).toMatchObject({
    headers: { Authorization: 'Bearer shared-view-code' },
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
  })
})
it('rejects late view-code responses and mismatched data-source config', async () => {
  let scope = { botId: '7', generation: 1, token: 'old-code' }
  const waiting = deferred()
  const authorize = createViewMetricsAuthorizer({
    getScope: () => scope,
    fetchImpl: () => waiting.promise,
  })
  const old = authorize(source(), { signal: signal() })
  scope = { ...scope, generation: 2, token: 'new-code' }
  waiting.resolve(
    response({ health: 'running', utilization_config: source(), utilization_access: grant() })
  )
  await expect(old).rejects.toMatchObject({ code: 'grant' })
  const mismatch = createViewMetricsAuthorizer({
    getScope: () => scope,
    fetchImpl: async () =>
      response({
        health: 'running',
        utilization_config: { ...source(), endpoint: 'https://wrong.example.invalid' },
        utilization_access: grant(),
      }),
  })
  await expect(mismatch(source(), { signal: signal() })).rejects.toMatchObject({ code: 'grant' })
})
it.each([401, 403, 404])(
  'revoked/expired view status %s invalidates the current view without new grants',
  async (status) => {
    const denied = vi.fn(),
      authorize = createViewMetricsAuthorizer({
        getScope: () => ({ botId: '7', generation: 1, token: 'code' }),
        denied,
        fetchImpl: async () => response(null, status),
      })
    await expect(authorize(source(), { signal: signal() })).rejects.toMatchObject({ code: 'grant' })
    expect(denied).toHaveBeenCalledTimes(1)
  }
)
it('honors a view authorization Retry-After date', async () => {
  const authorize = createViewMetricsAuthorizer({
    getScope: () => ({ botId: '7', generation: 1, token: 'code' }),
    fetchImpl: async () =>
      response(null, 429, { 'retry-after': new Date(Date.now() + 90000).toUTCString() }),
  })
  await expect(authorize(source(), { signal: signal() })).rejects.toMatchObject({
    code: 'limited',
    retryAfter: 90,
  })
})

it('rejects old authorization when the current view data source changes without changing its code', async () => {
  let scope = { botId: '7', generation: 1, token: 'same-code', source: source() }
  const waiting = deferred()
  const authorize = createViewMetricsAuthorizer({
    getScope: () => scope,
    fetchImpl: () => waiting.promise,
  })
  const result = authorize(source(), { signal: signal() })
  scope = { ...scope, source: { ...source(), endpoint: 'https://replacement.example.invalid' } }
  waiting.resolve(
    response({
      health: 'running',
      utilization_config: source(),
      utilization_access: grant('old-source'),
    })
  )
  await expect(result).rejects.toMatchObject({ code: 'grant' })
})

it('never reuses a cached grant after the current origin denies renewal', async () => {
  const item = collector()
  item.query.mockRejectedValue(new ProviderError('auth'))
  item.authorize.mockResolvedValueOnce(grant()).mockRejectedValue({ response: { status: 403 } })
  await expect(item.collect(signal())).rejects.toMatchObject({ code: 'grant' })
  expect(item.query).toHaveBeenCalledTimes(1)
  await expect(item.collect(signal())).rejects.toMatchObject({ code: 'grant' })
  expect(item.authorize).toHaveBeenCalledTimes(3)
  expect(item.query).toHaveBeenCalledTimes(1)
})
