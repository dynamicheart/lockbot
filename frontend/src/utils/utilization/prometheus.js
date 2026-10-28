// Browser-only provider. Never import the management or Agent API clients here.
export const DEFAULT_CONFIG = {
  id: '',
  name: 'Prometheus',
  type: 'prometheus',
  endpoint: '',
  nodeLabel: 'instance',
  deviceLabel: 'UUID',
  utilizationMetric: 'DCGM_FI_DEV_GPU_UTIL',
  memoryUsedMetric: 'DCGM_FI_DEV_FB_USED',
  memoryTotalMetric: 'DCGM_FI_DEV_FB_TOTAL',
  memoryUnit: 'MiB',
  nodeMapping: {},
  deviceMapping: {},
  intervalSeconds: 30,
  staleSeconds: 90,
}
const METRIC = /^[a-zA-Z_:][a-zA-Z0-9_:]*$/
const LABEL = /^[a-zA-Z_][a-zA-Z0-9_]*$/
const MAX_BYTES = 1024 * 1024
const MAX_SERIES = 2048
export class ProviderError extends Error {
  constructor(code, retryAfter = 0) {
    super(code)
    this.code = code
    this.retryAfter = retryAfter
  }
}
export function validateConfig(input) {
  const config = { ...DEFAULT_CONFIG, ...input }
  if (
    config.type !== 'prometheus' ||
    typeof config.name !== 'string' ||
    config.name.length > 64 ||
    !/^[a-zA-Z0-9_-]{0,64}$/.test(config.id)
  )
    throw new ProviderError('configuration')
  let url
  try {
    url = new URL(config.endpoint)
  } catch {
    throw new ProviderError('configuration')
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new ProviderError('configuration')
  }
  if (
    typeof location !== 'undefined' &&
    location.protocol === 'https:' &&
    url.protocol !== 'https:'
  ) {
    throw new ProviderError('https')
  }
  for (const name of ['utilizationMetric', 'memoryUsedMetric', 'memoryTotalMetric']) {
    if (!METRIC.test(config[name])) throw new ProviderError('configuration')
  }
  for (const name of ['nodeLabel', 'deviceLabel']) {
    if (!LABEL.test(config[name]) || config[name].startsWith('__'))
      throw new ProviderError('configuration')
  }
  if (config.nodeLabel === config.deviceLabel || !['B', 'MiB'].includes(config.memoryUnit)) {
    throw new ProviderError('configuration')
  }
  for (const mapping of [config.nodeMapping, config.deviceMapping]) {
    if (
      !mapping ||
      Array.isArray(mapping) ||
      typeof mapping !== 'object' ||
      Object.keys(mapping).length > 512
    ) {
      throw new ProviderError('configuration')
    }
    if (
      Object.entries(mapping).some(
        ([key, value]) => !key || typeof value !== 'string' || !value || value.length > 256
      )
    ) {
      throw new ProviderError('configuration')
    }
  }
  if (!Object.keys(config.nodeMapping).length) throw new ProviderError('configuration')
  config.endpoint = url.href.replace(/\/$/, '')
  config.intervalSeconds = Math.max(30, Math.min(3600, Number(config.intervalSeconds) || 30))
  config.staleSeconds = Math.max(30, Math.min(3600, Number(config.staleSeconds) || 90))
  // Explicit allowlist: no extra fields or credential values are persisted by callers.
  return Object.fromEntries(Object.keys(DEFAULT_CONFIG).map((key) => [key, config[key]]))
}
const quote = (value) => JSON.stringify(value)
const regexEscape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
export function selector(metric, config, nodes) {
  const targets = [...new Set(nodes.map((node) => config.nodeMapping[node]).filter(Boolean))]
  if (!targets.length || targets.length > 512) throw new ProviderError('mapping')
  return `${metric}{${config.nodeLabel}=~${quote(`^(?:${targets.map(regexEscape).join('|')})$`)}}`
}
const finite = (value) => {
  if (value == null || String(value).trim() === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}
function entries(body, config) {
  if (
    body?.status !== 'success' ||
    body.data?.resultType !== 'vector' ||
    !Array.isArray(body.data.result)
  ) {
    throw new ProviderError('format')
  }
  if (body.data.result.length > MAX_SERIES) throw new ProviderError('size')
  const values = new Map()
  for (const entry of body.data.result) {
    const node = entry.metric?.[config.nodeLabel]
    const device = entry.metric?.[config.deviceLabel]
    if (typeof node !== 'string' || typeof device !== 'string' || !node || !device) continue
    if (node.length > 256 || device.length > 256 || !Array.isArray(entry.value)) continue
    const key = JSON.stringify([node, device])
    // Duplicate identities must not silently pick the last series.
    values.set(key, values.has(key) ? null : finite(entry.value[1]))
  }
  return values
}
async function readJson(response) {
  if (Number(response.headers.get('content-length')) > MAX_BYTES) throw new ProviderError('size')
  if (!response.body?.getReader) throw new ProviderError('format')
  const reader = response.body.getReader()
  const chunks = []
  let size = 0
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > MAX_BYTES) throw new ProviderError('size')
      chunks.push(value)
    }
    const merged = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) {
      merged.set(chunk, offset)
      offset += chunk.length
    }
    return JSON.parse(new TextDecoder().decode(merged))
  } catch (error) {
    if (error instanceof ProviderError) throw error
    throw new ProviderError('format')
  } finally {
    await reader.cancel().catch(() => {})
  }
}
export async function collectPrometheus(
  input,
  nodes,
  { signal, bearer = '', fetchImpl = fetch, now = Date.now } = {}
) {
  const config = validateConfig(input)
  if (/^(?:lbr_|lbk_)/.test(bearer)) throw new ProviderError('credential')
  const headers = { Accept: 'application/json' }
  if (bearer) headers.Authorization = `Bearer ${bearer}`
  const results = {}
  const evaluationTime = String(now() / 1000)
  // Three metrics, each with a raw sample-timestamp query: six batched queries,
  // independent of resource count, at most two requests at once.
  for (const [kind, metric] of [
    ['compute', config.utilizationMetric],
    ['used', config.memoryUsedMetric],
    ['total', config.memoryTotalMetric],
  ]) {
    const raw = selector(metric, config, nodes)
    const query = async (expression) => {
      let response
      try {
        response = await fetchImpl(`${config.endpoint}/api/v1/query`, {
          method: 'POST',
          body: new URLSearchParams({ query: expression, time: evaluationTime, timeout: '4s' }),
          headers,
          credentials: 'omit',
          cache: 'no-store',
          redirect: 'error',
          referrerPolicy: 'no-referrer',
          signal,
        })
      } catch (error) {
        if (signal?.aborted) throw error
        throw new ProviderError('network')
      }
      if (!response.ok) {
        const seconds = Number(response.headers.get('retry-after'))
        const date = Date.parse(response.headers.get('retry-after'))
        const retry =
          Number.isFinite(seconds) && seconds > 0 ? seconds : Math.max(0, (date - now()) / 1000)
        throw new ProviderError(
          response.status === 401 || response.status === 403
            ? 'auth'
            : response.status === 429
              ? 'limited'
              : 'upstream',
          retry
        )
      }
      return entries(await readJson(response), config)
    }
    const [values, timestamps] = await Promise.all([query(raw), query(`timestamp(${raw})`)])
    results[kind] = { values, timestamps }
  }
  const keys = new Set(Object.values(results).flatMap((result) => [...result.values.keys()]))
  const cards = [...keys].map((key) => {
    const [node, device] = JSON.parse(key)
    const metrics = {}
    for (const kind of ['compute', 'used', 'total']) {
      let value = results[kind].values.get(key) ?? null
      let invalid = results[kind].values.has(key) && value == null
      const timestamp = results[kind].timestamps.get(key) ?? null
      if (value != null && kind !== 'compute' && config.memoryUnit === 'MiB') value *= 1024 ** 2
      if (
        value != null &&
        (value < 0 || (kind === 'compute' && value > 100) || (kind === 'total' && value <= 0))
      ) {
        invalid = true
        value = null
      }
      if (value != null && !Number.isFinite(value)) {
        invalid = true
        value = null
      }
      // Never use the instant-vector evaluation timestamp as the observation time.
      metrics[kind] = {
        value,
        invalid,
        observedAt: timestamp != null && timestamp > 0 ? timestamp * 1000 : null,
      }
    }
    if (metrics.used.value > metrics.total.value && metrics.total.value != null) {
      metrics.used.value = null
      metrics.used.invalid = true
    }
    return { node, device, metrics }
  })
  return { schema_version: 1, receivedAt: now(), cards }
}
export function metricStatus(metric, now, staleSeconds) {
  if (metric?.invalid) return 'invalid'
  if (metric?.value == null || !Number.isFinite(metric.observedAt) || metric.observedAt <= 0)
    return 'missing'
  if (metric.observedAt > now + 30000) return 'invalid'
  return now - metric.observedAt > staleSeconds * 1000 ? 'stale' : 'ok'
}
export function resolveCards(snapshot, config, nodeId, resourceId = null) {
  const node = config?.nodeMapping[nodeId]
  if (!node || !snapshot) return []
  const device = resourceId ? config.deviceMapping[resourceId] : null
  if (resourceId && !device) return []
  return snapshot.cards.filter(
    (card) => card.node === node && (!resourceId || card.device === device)
  )
}
export function configStorageKey(origin, instanceId, botId) {
  return `lockbot.metrics.v1:${JSON.stringify([origin, instanceId, String(botId)])}`
}

export const prometheusDatasource = {
  type: 'prometheus',
  query: collectPrometheus,
  async testDatasource(config, nodes, options) {
    const snapshot = await collectPrometheus(config, nodes, options)
    if (!snapshot.cards.length) throw new ProviderError('mapping')
    if (
      !snapshot.cards.some((card) =>
        Object.values(card.metrics).every(
          (metric) => metricStatus(metric, snapshot.receivedAt, config.staleSeconds) === 'ok'
        )
      )
    ) {
      throw new ProviderError('samples')
    }
    return snapshot
  },
}
