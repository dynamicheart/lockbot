import { describe, it, expect, vi } from 'vitest'
import {
  DEFAULT_CONFIG,
  validateConfig,
  selector,
  collectPrometheus,
  metricStatus,
  resolveCards,
  configStorageKey,
  prometheusDatasource,
} from './prometheus'
const at = 1800000000000
const config = () => ({
  ...DEFAULT_CONFIG,
  endpoint: 'https://metrics.example.test/prometheus',
  nodeMapping: { 'node-a': 'host.a:9400' },
  deviceMapping: { 'node-a:dev0': 'gpu-001' },
})
const series = (value, device = 'gpu-001', node = 'host.a:9400') => ({
  metric: { instance: node, UUID: device },
  value: [at / 1000, String(value)],
})
const response = (values) =>
  new Response(
    JSON.stringify({ status: 'success', data: { resultType: 'vector', result: values } }),
    { status: 200, headers: { 'content-type': 'application/json' } }
  )
function fakeFetch({ timestamp = at / 1000 - 2, compute = 0 } = {}) {
  return vi.fn(async (_url, options) => {
    const expression = options.body.get('query')
    const value = expression.startsWith('timestamp(')
      ? timestamp
      : expression.startsWith('DCGM_FI_DEV_GPU_UTIL')
        ? compute
        : expression.startsWith('DCGM_FI_DEV_FB_USED')
          ? 1024
          : 2048
    return response([series(value), series(value, 'gpu-other', 'unrequested-node')])
  })
}
describe('browser Prometheus provider', () => {
  it('queries a fixed batch and reads actual raw sample timestamps instead of evaluation time', async () => {
    const fetchImpl = fakeFetch({ timestamp: at / 1000 - 120 })
    const result = await collectPrometheus(config(), ['node-a'], { fetchImpl, now: () => at })
    expect(fetchImpl).toHaveBeenCalledTimes(6)
    const card = resolveCards(result, config(), 'node-a', 'node-a:dev0')[0]
    expect(card.metrics.compute.value).toBe(0)
    expect(card.metrics.compute.observedAt).toBe(at - 120000)
    expect(metricStatus(card.metrics.compute, at, 90)).toBe('stale')
    expect(card.metrics.used.value).toBe(1024 ** 3)
    expect(card.metrics.total.value).toBe(2 * 1024 ** 3)
    expect(resolveCards(result, config(), 'node-a', 'node-a:dev1')).toEqual([])
    expect(resolveCards(result, config(), 'another-node')).toEqual([])
  })
  it('only uses an explicitly supplied source token and disables all ambient credentials', async () => {
    const fetchImpl = fakeFetch()
    await collectPrometheus(config(), ['node-a'], {
      fetchImpl,
      bearer: 'source-only-token',
      now: () => at,
    })
    for (const [url, request] of fetchImpl.mock.calls) {
      expect(url).toBe('https://metrics.example.test/prometheus/api/v1/query')
      expect(url).not.toContain('source-only-token')
      expect(request.headers.Authorization).toBe('Bearer source-only-token')
      expect(request.credentials).toBe('omit')
      expect(request.redirect).toBe('error')
      expect(request.referrerPolicy).toBe('no-referrer')
      expect([...request.body.keys()].sort()).toEqual(['query', 'time', 'timeout'])
    }
    const noAuth = fakeFetch()
    await collectPrometheus(config(), ['node-a'], { fetchImpl: noAuth, now: () => at })
    expect(noAuth.mock.calls[0][1].headers.Authorization).toBeUndefined()
    await expect(
      collectPrometheus(config(), ['node-a'], { fetchImpl, bearer: 'lbr_private' })
    ).rejects.toMatchObject({ code: 'credential' })
  })
  it('rejects credentials in URLs and persists only allowlisted nonsecret configuration', () => {
    for (const endpoint of [
      'https://user:secret@metrics.test',
      'https://metrics.test?token=secret',
      'file:///etc/passwd',
    ]) {
      expect(() => validateConfig({ ...config(), endpoint })).toThrow()
    }
    const checked = validateConfig({ ...config(), bearer: 'never-save', jwt: 'never-save' })
    expect(JSON.stringify(checked)).not.toContain('never-save')
    expect(configStorageKey('https://one.test', 'instance-a', 1)).not.toBe(
      configStorageKey('https://one.test', 'instance-b', 1)
    )
    expect(configStorageKey('https://one.test', 'instance-a', 1)).not.toBe(
      configStorageKey('https://one.test', 'instance-a', 2)
    )
  })
  it('escapes label values as regex literals and as PromQL strings', () => {
    const c = { ...config(), nodeMapping: { a: 'host.*"\\\n' } }
    const expression = selector('gpu_metric', c, ['a'])
    const encoded = expression.slice(expression.indexOf('=~') + 2, -1)
    expect(JSON.parse(encoded)).toBe('^(?:host\\.\\*"\\\\\n)$')
    expect(() => selector('gpu_metric', c, ['missing'])).toThrow()
    expect(() => validateConfig({ ...config(), deviceLabel: 'same', nodeLabel: 'same' })).toThrow()
  })
  it.each(['NaN', '+Inf', '-Inf', '-1', '101', ''])(
    'does not turn invalid utilization %s into idle',
    async (value) => {
      const result = await collectPrometheus(config(), ['node-a'], {
        fetchImpl: fakeFetch({ compute: value }),
        now: () => at,
      })
      expect(
        resolveCards(result, config(), 'node-a', 'node-a:dev0')[0].metrics.compute.value
      ).toBeNull()
    }
  )
  it('duplicate identities and missing sample timestamps stay unknown', async () => {
    const fetchImpl = vi.fn(async (_url, options) =>
      options.body.get('query').startsWith('timestamp(')
        ? response([])
        : response([series(4), series(9)])
    )
    const result = await collectPrometheus(config(), ['node-a'], { fetchImpl, now: () => at })
    const metric = result.cards[0].metrics.compute
    expect(metric.value).toBeNull()
    expect(metricStatus(metric, at, 90)).toBe('invalid')
    expect(metricStatus({ value: 1, observedAt: at + 31000 }, at, 90)).toBe('invalid')
  })
  it('does not match cards by array order, returns partial metrics without guessing', async () => {
    const fetchImpl = vi.fn(async (_url, options) => {
      const query = options.body.get('query')
      if (query.startsWith('timestamp('))
        return response([series(at / 1000 - 1, 'gpu-002'), series(at / 1000 - 1)])
      if (query.startsWith('DCGM_FI_DEV_FB_TOTAL')) return response([])
      return response([series(80, 'gpu-002'), series(20)])
    })
    const result = await collectPrometheus(config(), ['node-a'], { fetchImpl, now: () => at })
    const card = resolveCards(result, config(), 'node-a', 'node-a:dev0')[0]
    expect(card.device).toBe('gpu-001')
    expect(card.metrics.compute.value).toBe(20)
    expect(card.metrics.total.value).toBeNull()
    await expect(
      prometheusDatasource.testDatasource(config(), ['node-a'], { fetchImpl, now: () => at })
    ).rejects.toMatchObject({ code: 'samples' })
  })
  it('rejects values that overflow during byte-unit conversion', async () => {
    const fetchImpl = vi.fn(async (_url, options) => {
      const query = options.body.get('query')
      return response([
        series(
          query.startsWith('timestamp(')
            ? at / 1000
            : query.startsWith('DCGM_FI_DEV_FB_TOTAL')
              ? '1e308'
              : '1'
        ),
      ])
    })
    const result = await collectPrometheus(config(), ['node-a'], { fetchImpl, now: () => at })
    expect(result.cards[0].metrics.total.value).toBeNull()
    expect(metricStatus(result.cards[0].metrics.total, at, 90)).toBe('invalid')
  })
  it('tests query schema and fresh metric values, not a health endpoint', async () => {
    const fetchImpl = fakeFetch()
    await expect(
      prometheusDatasource.testDatasource(config(), ['node-a'], { fetchImpl, now: () => at })
    ).resolves.toHaveProperty('schema_version', 1)
    expect(fetchImpl.mock.calls.every(([url]) => url.endsWith('/api/v1/query'))).toBe(true)
    await expect(
      collectPrometheus(config(), ['node-a'], { fetchImpl: async () => new Response('{}') })
    ).rejects.toMatchObject({ code: 'format' })
  })
  it('bounds response bytes and series, reports rate and auth errors without raw server text', async () => {
    await expect(
      collectPrometheus(config(), ['node-a'], {
        fetchImpl: async () => new Response('private body', { status: 401 }),
      })
    ).rejects.toMatchObject({ code: 'auth' })
    await expect(
      collectPrometheus(config(), ['node-a'], {
        fetchImpl: async () => new Response('', { status: 429, headers: { 'retry-after': '75' } }),
      })
    ).rejects.toMatchObject({ code: 'limited', retryAfter: 75 })
    await expect(
      collectPrometheus(config(), ['node-a'], {
        fetchImpl: async () => new Response('x'.repeat(1024 * 1024 + 1)),
      })
    ).rejects.toMatchObject({ code: 'size' })
    await expect(
      collectPrometheus(config(), ['node-a'], {
        fetchImpl: async () => response(Array.from({ length: 2049 }, () => series(1))),
      })
    ).rejects.toMatchObject({ code: 'size' })
  })
})
