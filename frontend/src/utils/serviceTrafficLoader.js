/** Keep one scope's telemetry distinct from previous or late request results. */
export function createServiceTrafficLoader({ request, publish, canRequest = () => true }) {
  let scope
  let scopeKey
  let generation = 0
  let controller = null
  let disposed = false

  async function refresh() {
    if (disposed || controller || !canRequest() || !scope) return false
    const current = ++generation
    const active = new AbortController()
    controller = active
    publish({ loading: true })
    try {
      const snapshot = await request(scope, active.signal)
      if (!disposed && current === generation) publish({ snapshot, failed: false })
    } catch (error) {
      if (
        !disposed &&
        current === generation &&
        error?.code !== 'ERR_CANCELED' &&
        error?.name !== 'AbortError'
      )
        publish({ failed: true })
    } finally {
      if (!disposed && current === generation) {
        controller = null
        publish({ loading: false })
      }
    }
    return true
  }

  function setScope(next) {
    const key = JSON.stringify(next)
    if (key !== scopeKey) {
      generation++
      controller?.abort()
      controller = null
      scope = { ...next }
      scopeKey = key
      publish({ snapshot: null, failed: false, loading: false })
    }
    return refresh()
  }

  function dispose() {
    disposed = true
    generation++
    controller?.abort()
    controller = null
  }

  return { refresh, setScope, dispose }
}
