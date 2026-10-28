import { describe, expect, it } from 'vitest'
import { formatActivityTime } from './activityDisplay'

describe('Agent activity local time', () => {
  it('treats naive UTC microseconds and explicit UTC as the same instant', () => {
    const local = formatActivityTime('2026-10-27T04:59:32.288134', 'zh-CN', 'Asia/Shanghai')
    expect(local).toBe('2026/10/27 12:59:32')
    expect(formatActivityTime('2026-10-27T04:59:32.288Z', 'zh-CN', 'Asia/Shanghai')).toBe(local)
  })
  it('respects both positive and negative explicit offsets', () => {
    const expected = formatActivityTime('2026-10-27T04:59:32Z', 'en-US', 'Asia/Shanghai')
    expect(formatActivityTime('2026-10-27T12:59:32+08:00', 'en-US', 'Asia/Shanghai')).toBe(expected)
    expect(formatActivityTime('2026-10-26T21:59:32-07:00', 'en-US', 'Asia/Shanghai')).toBe(expected)
    expect(expected).toContain('12:59:32')
  })
  it('handles missing and invalid timestamps without printing raw server values', () => {
    for (const value of [null, undefined, '', 'bad timestamp']) {
      expect(formatActivityTime(value, 'zh-CN')).toBe('—')
    }
  })
})
