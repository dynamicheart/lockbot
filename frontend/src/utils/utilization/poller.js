// One cancellable collection; hidden/disabled/permission-lost views do no work.
export function createMetricsPoller({
  collect,
  publish,
  now = () => performance.now(),
  wallNow = Date.now,
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
  let startedAtWall = 0
  let interval = 30000
  let previousWall = wallNow(),
    previousMono = now()
  const reconcileSleep = () => {
    const mono = now(),
      wall = wallNow()
    const suspended = Math.max(0, wall - previousWall - (mono - previousMono))
    due = Math.max(0, due - suspended)
    previousWall = wall
    previousMono = mono
  }
  const schedule = () => {
    clearTimer(wake)
    if (active) wake = timer(() => refresh(), Math.max(0, due - now()))
  }
  async function refresh() {
    reconcileSleep()
    if (!active || busy) return false
    if (now() < due) {
      schedule()
      return false
    }
    busy = true
    startedAtWall = wallNow()
    const deadlineAt = startedAtWall + 15000
    due = now() + interval
    const run = generation
    const currentController = new AbortController()
    controller = currentController
    const currentTimeout = timer(() => currentController.abort(), 15000)
    timeout = currentTimeout
    publish({ loading: true })
    try {
      const snapshot = await collect(currentController.signal, { deadlineAt, wallNow })
      if (run !== generation || !active) return false
      reconcileSleep()
      if (currentController.signal.aborted || wallNow() >= deadlineAt) {
        currentController.abort()
        throw new Error('timeout')
      }
      failures = 0
      due = now() + interval
      publish({ loading: false, snapshot, error: null })
    } catch (error) {
      if (run !== generation || !active) return false
      reconcileSleep()
      if (wallNow() >= deadlineAt) currentController.abort()
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
      reconcileSleep()
      due = Math.max(due, stamp)
    },
    start(seconds = 30) {
      reconcileSleep()
      interval = Math.max(30000, seconds * 1000)
      // A visible tab may sleep without a visibility event. Do not resume a collection
      // whose total deadline elapsed while its timer/monotonic clock was suspended.
      if (busy && wallNow() - startedAtWall >= 15000) {
        generation++
        controller?.abort()
        clearTimer(timeout)
        busy = false
        publish({ loading: false })
      }
      if (active) {
        schedule()
        return
      }
      active = true
      schedule()
    },
    stop() {
      reconcileSleep()
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
