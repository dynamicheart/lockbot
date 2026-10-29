// One cancellable collection; hidden/disabled/permission-lost views do no work.
export function createMetricsPoller({
  collect,
  publish,
  now = () => performance.now(),
  timer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let active = false
  let busy = false
  let generation = 0
  let controller
  let wake
  let timeout
  let due = 0
  let failures = 0
  let interval = 30000
  const schedule = () => {
    clearTimer(wake)
    if (active) wake = timer(() => refresh(), Math.max(0, due - now()))
  }
  async function refresh() {
    if (!active || busy) return false
    if (now() < due) {
      schedule()
      return false
    }
    busy = true
    due = now() + interval
    const run = generation
    const currentController = new AbortController()
    controller = currentController
    const currentTimeout = timer(() => currentController.abort(), 15000)
    timeout = currentTimeout
    publish({ loading: true })
    try {
      const snapshot = await collect(currentController.signal)
      if (run !== generation || !active) return false
      failures = 0
      due = now() + interval
      publish({ loading: false, snapshot, error: null })
    } catch (error) {
      if (run !== generation || !active) return false
      failures++
      due =
        now() +
        Math.max(
          Math.min(interval * 2 ** Math.min(failures - 1, 4), 300000),
          (error.retryAfter || 0) * 1000
        )
      publish({
        loading: false,
        error: currentController.signal.aborted ? 'timeout' : error.code || 'network',
      })
    } finally {
      clearTimer(currentTimeout)
      currentController.abort()
      if (run === generation) {
        busy = false
        schedule()
      }
    }
    return true
  }
  return {
    refresh,
    deferUntil(stamp) {
      due = Math.max(due, stamp)
    },
    start(seconds = 30) {
      interval = Math.max(30000, seconds * 1000)
      if (active) return
      active = true
      schedule()
    },
    stop() {
      active = false
      generation++
      controller?.abort()
      clearTimer(wake)
      clearTimer(timeout)
      busy = false
      publish({ loading: false })
    },
  }
}
