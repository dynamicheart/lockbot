import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createMetricsPoller } from './poller'
const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}
describe('metrics poll lifecycle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())
  it('does no work before enable, coalesces manual refresh and respects minimum interval', async () => {
    const collect = vi.fn(async () => ({ cards: [] }))
    const poll = createMetricsPoller({ collect, publish: vi.fn(), now: () => Date.now() })
    await vi.advanceTimersByTimeAsync(60000)
    expect(collect).not.toHaveBeenCalled()
    poll.start(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(collect).toHaveBeenCalledTimes(1)
    expect(await poll.refresh()).toBe(false)
    await vi.advanceTimersByTimeAsync(29999)
    expect(collect).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(collect).toHaveBeenCalledTimes(2)
    poll.stop()
  })
  it('cancels a hidden/expired/disabled view and ignores its late response after restart', async () => {
    let finish
    const calls = []
    const publish = vi.fn()
    const collect = vi.fn((signal) => {
      calls.push(signal)
      return new Promise((resolve) => {
        finish = resolve
      })
    })
    const poll = createMetricsPoller({ collect, publish, now: () => Date.now() })
    poll.start()
    await vi.advanceTimersByTimeAsync(0)
    poll.stop()
    expect(calls[0].aborted).toBe(true)
    finish({ privateOldSnapshot: true })
    await flush()
    expect(publish.mock.calls.some(([update]) => update.snapshot?.privateOldSnapshot)).toBe(false)
    await vi.advanceTimersByTimeAsync(120000)
    expect(collect).toHaveBeenCalledTimes(1)
  })
  it('uses one in-flight request and aborts collection on timeout', async () => {
    let signal
    const collect = vi.fn((current) => {
      signal = current
      return new Promise((_, reject) =>
        current.addEventListener('abort', () => reject(new Error('abort')))
      )
    })
    const publish = vi.fn()
    const poll = createMetricsPoller({ collect, publish, now: () => Date.now() })
    poll.start()
    await vi.advanceTimersByTimeAsync(0)
    await poll.refresh()
    expect(collect).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(15000)
    expect(signal.aborted).toBe(true)
    expect(publish).toHaveBeenCalledWith({ loading: false, error: 'timeout' })
    poll.stop()
  })
  it('does not let hide/show cancellation bypass the polling floor', async () => {
    const collect = vi.fn(
      (signal) =>
        new Promise((_, reject) =>
          signal.addEventListener('abort', () => reject(new Error('abort')))
        )
    )
    const poll = createMetricsPoller({ collect, publish: vi.fn(), now: () => Date.now() })
    poll.start()
    await vi.advanceTimersByTimeAsync(0)
    poll.stop()
    poll.start()
    await vi.advanceTimersByTimeAsync(29999)
    expect(collect).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(collect).toHaveBeenCalledTimes(2)
    poll.stop()
  })
  it('backs off failures and honors upstream Retry-After even on manual refresh', async () => {
    const collect = vi.fn(async () => {
      throw { code: 'limited', retryAfter: 90 }
    })
    const poll = createMetricsPoller({ collect, publish: vi.fn(), now: () => Date.now() })
    poll.start()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(89999)
    await poll.refresh()
    expect(collect).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(collect).toHaveBeenCalledTimes(2)
    poll.stop()
  })
  it('keeps the post-test and hidden-tab refresh floor when restarted', async () => {
    const collect = vi.fn(async () => ({}))
    const poll = createMetricsPoller({ collect, publish: vi.fn(), now: () => Date.now() })
    poll.deferUntil(Date.now() + 30000)
    poll.start()
    await vi.advanceTimersByTimeAsync(10000)
    expect(collect).not.toHaveBeenCalled()
    poll.stop()
    poll.start()
    await vi.advanceTimersByTimeAsync(19999)
    expect(collect).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(collect).toHaveBeenCalledTimes(1)
    poll.stop()
  })
})
