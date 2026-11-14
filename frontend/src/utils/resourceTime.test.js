import { execFileSync } from 'node:child_process'
import process from 'node:process'
import { expect, it } from 'vitest'
import { formatResourceTime } from './resourceTime'

const instant = '2026-11-14T00:15:30Z'
it('uses explicit Shanghai time for Chinese resource timestamps', () => {
  expect(formatResourceTime(instant, 'zh-CN')).toBe('2026-11-14 08:15:30 UTC+08:00')
  expect(formatResourceTime(Date.parse(instant), 'zh')).toBe('2026-11-14 08:15:30 UTC+08:00')
})
it('uses explicit UTC for English instead of an implicit browser zone', () => {
  expect(formatResourceTime(instant, 'en')).toBe('2026-11-14 00:15:30 UTC')
  expect(formatResourceTime('2026-11-13T17:15:30-07:00', 'en-US')).toBe('2026-11-14 00:15:30 UTC')
})
it('interprets naive backend timestamps as UTC without mutating Date inputs', () => {
  expect(formatResourceTime('2026-11-14T00:15:30', 'zh')).toBe('2026-11-14 08:15:30 UTC+08:00')
  expect(formatResourceTime('2026-11-14 00:15:30.000', 'en')).toBe('2026-11-14 00:15:30 UTC')
  const date = new Date(instant)
  const epoch = date.getTime()
  expect(formatResourceTime(date, 'zh')).toBe('2026-11-14 08:15:30 UTC+08:00')
  expect(date.getTime()).toBe(epoch)
})
it.each([undefined, null, '', 'invalid', Number.NaN])(
  'shows missing/invalid input %s as unknown',
  (value) => {
    expect(formatResourceTime(value, 'zh')).toBe('—')
  }
)
it.each(['UTC', 'America/Los_Angeles', 'Asia/Shanghai'])(
  'is stable when the host timezone is %s',
  (TZ) => {
    const source = new URL('./resourceTime.js', import.meta.url).href
    const script = `import { formatResourceTime } from ${JSON.stringify(source)}; console.log(JSON.stringify([formatResourceTime(${JSON.stringify(instant)}, 'zh'), formatResourceTime('2026-11-14T00:15:30', 'en'), formatResourceTime(0, 'en')]))`
    const result = JSON.parse(
      execFileSync(process.execPath, ['--input-type=module', '-e', script], {
        env: { ...process.env, TZ },
        encoding: 'utf8',
      })
    )
    expect(result).toEqual([
      '2026-11-14 08:15:30 UTC+08:00',
      '2026-11-14 00:15:30 UTC',
      '1970-01-01 00:00:00 UTC',
    ])
  }
)
