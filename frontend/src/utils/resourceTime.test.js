import { execFileSync } from 'node:child_process'
import process from 'node:process'
import { expect, it } from 'vitest'
import { formatResourceTime } from './resourceTime'

const instant = '2026-11-14T00:15:30Z'
it('uses an explicit preferred timezone independently of language', () => {
  for (const locale of ['zh-CN', 'en']) {
    expect(formatResourceTime(instant, locale, 'Asia/Shanghai')).toBe(
      '2026-11-14 08:15:30 UTC+08:00'
    )
    expect(formatResourceTime(instant, locale, 'UTC')).toBe('2026-11-14 00:15:30 UTC')
  }
})
it('preserves the instant in epoch, offset and legacy naive input', () => {
  for (const value of [
    instant,
    Date.parse(instant),
    '2026-11-13T16:15:30-08:00',
    '2026-11-14T00:15:30',
    '2026-11-14 00:15:30.000',
  ]) {
    expect(formatResourceTime(value, 'en', 'UTC')).toBe('2026-11-14 00:15:30 UTC')
  }
  const date = new Date(instant)
  const epoch = date.getTime()
  expect(formatResourceTime(date, 'zh', 'Asia/Shanghai')).toBe('2026-11-14 08:15:30 UTC+08:00')
  expect(date.getTime()).toBe(epoch)
})
it('uses the correct date and offset for winter, summer and fractional zones', () => {
  expect(formatResourceTime(instant, 'zh', 'America/Los_Angeles')).toBe(
    '2026-11-13 16:15:30 UTC-08:00'
  )
  expect(formatResourceTime('2026-07-01T00:15:30Z', 'en', 'America/Los_Angeles')).toBe(
    '2026-06-30 17:15:30 UTC-07:00'
  )
  expect(formatResourceTime(instant, 'en', 'Asia/Kathmandu')).toBe('2026-11-14 06:00:30 UTC+05:45')
  expect(formatResourceTime(instant, 'zh', 'Pacific/Chatham')).toBe('2026-11-14 14:00:30 UTC+13:45')
})
it.each([undefined, null, '', 'invalid', Number.NaN])(
  'shows missing/invalid input %s as unknown',
  (value) => {
    expect(formatResourceTime(value, 'zh')).toBe('—')
  }
)
it('does not invent a display time for an invalid explicit timezone', () => {
  expect(formatResourceTime(instant, 'en', 'Invalid/Zone')).toBe('—')
})
it.each([
  ['UTC', '2026-11-14 00:15:30 UTC'],
  ['America/Los_Angeles', '2026-11-13 16:15:30 UTC-08:00'],
  ['Asia/Shanghai', '2026-11-14 08:15:30 UTC+08:00'],
])('follows browser timezone %s without deriving it from the UI language', (TZ, expected) => {
  const source = new URL('./resourceTime.js', import.meta.url).href
  const script = `import { formatResourceTime } from ${JSON.stringify(source)}; console.log(JSON.stringify([formatResourceTime(${JSON.stringify(instant)}, 'zh'), formatResourceTime(${JSON.stringify(instant)}, 'en'), formatResourceTime(${JSON.stringify(instant)}, 'zh', 'UTC')]))`
  const result = JSON.parse(
    execFileSync(process.execPath, ['--input-type=module', '-e', script], {
      env: { ...process.env, TZ },
      encoding: 'utf8',
    })
  )
  expect(result).toEqual([expected, expected, '2026-11-14 00:15:30 UTC'])
})
