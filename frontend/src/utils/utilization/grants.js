import { ProviderError, validateConfig, retryAfterSeconds as retryDelay } from './prometheus'

export function linkSignals(...signals) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  for (const signal of signals.filter(Boolean)) {
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
  }
  return {
    signal: controller.signal,
    abort,
    dispose: () =>
      signals.filter(Boolean).forEach((signal) => signal.removeEventListener('abort', abort)),
  }
}
function check(signal) {
  if (signal?.aborted) throw new ProviderError('timeout')
}
export function abortable(promise, signal) {
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (callback, value) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      callback(value)
    }
    const abort = () => finish(reject, new ProviderError('timeout'))
    signal?.addEventListener('abort', abort, { once: true })
    // Always observe the supplied promise, even when it ignores cancellation.
    Promise.resolve(promise).then(
      (value) => {
        if (signal?.aborted) abort()
        else finish(resolve, value)
      },
      (error) => finish(reject, error)
    )
    if (signal?.aborted) abort()
  })
}
function valid(grant, now, margin = 0) {
  return (
    grant?.scope === 'metrics:read' &&
    typeof grant.token === 'string' &&
    !!grant.token &&
    Number.isFinite(grant.expires_at) &&
    grant.expires_at * 1000 > now + margin
  )
}
export function datasourceIdentity(source) {
  const config = validateConfig(source)
  for (const key of ['nodeMapping', 'deviceMapping'])
    config[key] = Object.fromEntries(
      Object.entries(config[key]).sort(([a], [b]) => a.localeCompare(b))
    )
  return JSON.stringify(config)
}

/** One collection budget covers short authorization and at most one auth retry. */
export function createGrantedMetricsCollector({
  getConfig,
  getNodes,
  getBearer,
  authorize,
  query,
  now = Date.now,
}) {
  let cached
  let identity
  let generation = 0
  async function collect(signal) {
    const config = getConfig(),
      nodes = [...getNodes()]
    const scope = JSON.stringify([datasourceIdentity(config), nodes])
    if (identity !== scope) {
      cached = null
      identity = scope
      generation++
    }
    const run = generation
    const guard = () => {
      check(signal)
      if (run !== generation) throw new ProviderError('grant')
    }
    if (config.accessMode !== 'short_lived')
      return query(config, nodes, { signal, bearer: getBearer() || '' })
    const renew = async (force = false) => {
      guard()
      if (!force) {
        if (valid(cached, now(), 30000)) return cached
      }
      // A rejected renewal must never fall back to a previously cached grant.
      cached = null
      let grant
      try {
        grant = await abortable(authorize(config, { signal, force }), signal)
      } catch (error) {
        if (error instanceof ProviderError) throw error
        if (error.response?.status === 429)
          throw new ProviderError(
            'limited',
            retryDelay(error.response.headers?.['retry-after'], now())
          )
        throw new ProviderError('grant')
      }
      guard()
      if (!valid(grant, now())) throw new ProviderError('grant')
      cached = grant
      return grant
    }
    const attempt = async (grant) => {
      guard()
      const linked = linkSignals(signal)
      try {
        const result = await abortable(
          query(config, nodes, { signal: linked.signal, bearer: grant.token }),
          signal
        )
        guard()
        return result
      } catch (error) {
        if (error.code === 'auth' && run === generation) cached = null
        throw error
      } finally {
        linked.abort()
        linked.dispose()
      }
    }
    const grant = await renew()
    try {
      return await attempt(grant)
    } catch (error) {
      guard()
      if (error.code !== 'auth' || error.retryAfter > 0) throw error
      return attempt(await renew(true))
    }
  }
  return {
    collect,
    reset: () => {
      generation++
      cached = null
      identity = null
    },
  }
}

/** Uses only the current view capability at the same Lockbot origin. */
export function createViewMetricsAuthorizer({ getScope, denied = () => {}, fetchImpl = fetch }) {
  return async (source, { signal, force = false } = {}) => {
    check(signal)
    const scope = getScope()
    if (!scope?.token) throw new ProviderError('grant')
    if (
      !force &&
      valid(scope.access, Date.now(), 30000) &&
      datasourceIdentity(scope.source) === datasourceIdentity(source)
    )
      return scope.access
    const current = () => {
      const latest = getScope()
      return (
        latest?.generation === scope.generation &&
        latest?.botId === scope.botId &&
        latest?.token === scope.token &&
        (!latest.source || datasourceIdentity(latest.source) === datasourceIdentity(source))
      )
    }
    const response = await abortable(
      fetchImpl(`/api/bot-views/${encodeURIComponent(scope.botId)}/state`, {
        headers: { Authorization: `Bearer ${scope.token}` },
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        signal,
      }),
      signal
    )
    check(signal)
    if (!current()) throw new ProviderError('grant')
    if ([401, 403, 404].includes(response.status)) {
      denied()
      throw new ProviderError('grant')
    }
    if (!response.ok)
      throw new ProviderError(
        response.status === 429 ? 'limited' : 'grant',
        retryDelay(response.headers.get('retry-after'))
      )
    const data = await abortable(response.json(), signal)
    check(signal)
    if (
      !current() ||
      data.health !== 'running' ||
      datasourceIdentity(data.utilization_config) !== datasourceIdentity(source)
    )
      throw new ProviderError('grant')
    return data.utilization_access
  }
}
