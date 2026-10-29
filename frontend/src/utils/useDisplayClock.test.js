import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer } from 'vue'
import { useDisplayClock } from './useDisplayClock'
import { metricStatus } from './utilization/prometheus'

// Mount the actual lifecycle hooks without a browser or a networked BotDetail page.
const renderer = createRenderer({
  createComment: () => ({}),
  insert: () => {},
  remove: () => {},
  parentNode: () => null,
  nextSibling: () => null,
})
const started = 1800000000000
let app
function mountClock() {
  let clock
  app = renderer.createApp({
    setup() {
      clock = useDisplayClock()
      return () => null
    },
  })
  app.mount({})
  return clock
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(started)
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
})

describe('metrics display time', () => {
  it('accepts a newly collected sample after the page has stayed mounted', () => {
    const clock = mountClock()
    vi.advanceTimersByTime(300000)
    const fresh = { value: 43, observedAt: Date.now() }
    // A mount-time default would incorrectly flag this fresh five-minute sample.
    expect(metricStatus(fresh, started, 90)).toBe('invalid')
    expect(metricStatus(fresh, clock.value, 90)).toBe('ok')
    expect(clock.value).toBe(Date.now())
  })

  it('ages an unchanged snapshot while polling is paused or the source is lost', () => {
    const clock = mountClock()
    const cached = { value: 0, observedAt: started }
    expect(metricStatus(cached, clock.value, 90)).toBe('ok')
    // No collect or refresh occurs: only local presentation time advances.
    vi.advanceTimersByTime(91000)
    expect(metricStatus(cached, clock.value, 90)).toBe('stale')
  })

  it('cleans up its local timer when the detail page unmounts', () => {
    const clock = mountClock()
    expect(vi.getTimerCount()).toBe(1)
    app.unmount()
    app = null
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(300000)
    expect(clock.value).toBe(started)
  })
})
