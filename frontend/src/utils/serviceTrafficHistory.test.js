import { expect, it, vi } from 'vitest'
import {
  createTrafficHistoryLoader,
  historyEndpoint,
  historyRange,
  historyRows,
  operationCount,
} from './serviceTrafficHistory'
const now = Date.parse('2026-11-14T08:00:00Z')
it('requests explicit UTC ranges and bounded bucket sizes for each preset', () => {
  expect(historyRange('24h', [], now)).toEqual({
    start: '2026-11-13T08:00:00.000Z',
    end: '2026-11-14T08:00:00.000Z',
    resolution_seconds: 900,
  })
  expect(historyRange('7d', [], now).resolution_seconds).toBe(3600)
  expect(historyRange('30d', [], now).resolution_seconds).toBe(21600)
  expect(
    historyRange('custom', ['2026-11-14T10:00:00+08:00', '2026-11-14T11:00:00+08:00'], now).start
  ).toBe('2026-11-14T02:00:00.000Z')
})
it.each([
  [],
  ['bad', 'bad'],
  ['2026-10-01', '2026-11-14'],
  ['2026-11-14', '2026-11-13'],
  ['2026-11-13', '2026-11-15'],
])('rejects invalid/custom range %s before HTTP', (range) => {
  expect(historyRange('custom', range, now)).toBeNull()
})
it('keeps bot reads on the bot endpoint and admin reads on the admin endpoint', () => {
  expect(historyEndpoint(7, false)).toBe('/bots/7/service-traffic/history')
  expect(historyEndpoint(7, true)).toBe('/admin/service-traffic/history')
})
it('distinguishes unknown and partial buckets from confirmed zero', () => {
  const rows = historyRows({
    history: [
      { coverage: 'unknown', observed: false, requests: null, operations: null },
      {
        coverage: 'complete',
        observed: true,
        requests: 0,
        operations: { chat: 0, agent: 0, operator: 0 },
      },
      {
        coverage: 'partial',
        observed: true,
        requests: 4,
        operations: { chat: 1, agent: 2, operator: 0 },
      },
    ],
  })
  expect(rows.map((point) => point.known)).toEqual([false, true, true])
  expect(operationCount(rows[2])).toBe(3)
})
it('discards late results and failures after a bot or range changes', async () => {
  const pending = []
  const publish = vi.fn()
  const loader = createTrafficHistoryLoader({
    request: (scope, signal) =>
      new Promise((resolve, reject) => pending.push({ scope, signal, resolve, reject })),
    publish,
  })
  const old = loader.setScope({ botId: 1, start: 'old' })
  const newer = loader.setScope({ botId: 2, start: 'new' })
  expect(pending[0].signal.aborted).toBe(true)
  pending[1].resolve({ botId: 2 })
  await newer
  pending[0].resolve({ botId: 1 })
  await old
  expect(publish.mock.calls.filter(([value]) => value.snapshot)).toEqual([
    [{ snapshot: { botId: 2 }, failed: false }],
  ])
  const oldRange = loader.setScope({ botId: 2, start: 'old' })
  const newRange = loader.setScope({ botId: 2, start: 'newer' })
  pending[3].resolve({ range: 'newer' })
  await newRange
  pending[2].reject(new Error('old failure'))
  await oldRange
  expect(publish).not.toHaveBeenCalledWith({ failed: true })
  loader.dispose()
})
it('invalid ranges clear data and cancel reads without requesting an empty scope', async () => {
  let resolve
  const request = vi.fn(
    () =>
      new Promise((done) => {
        resolve = done
      })
  )
  const publish = vi.fn()
  const loader = createTrafficHistoryLoader({ request, publish })
  const pending = loader.setScope({ botId: 1, start: 'old' })
  await loader.setScope(null)
  expect(request).toHaveBeenCalledTimes(1)
  resolve({ old: true })
  await pending
  expect(publish).not.toHaveBeenCalledWith({ snapshot: { old: true }, failed: false })
  expect(publish).toHaveBeenCalledWith({ snapshot: null, failed: false, loading: false })
  loader.dispose()
})
