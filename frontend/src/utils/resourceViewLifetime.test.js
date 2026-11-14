import { expect, it } from 'vitest'
import { resourceViewDeadline } from './resourceViewLifetime'

it('treats explicit null as permanent without applying a local clock offset', () => {
  expect(resourceViewDeadline(null, 12345)).toBe(0)
})

it('keeps finite link deadlines aligned to server time', () => {
  const expiry = '2026-12-14T04:00:00+00:00'
  expect(resourceViewDeadline(expiry, 30000)).toBe(Date.parse(expiry) - 30000)
})

it.each([undefined, '', 'invalid', 0])(
  'does not turn malformed expiry %s into permanent access',
  (expiry) => {
    expect(resourceViewDeadline(expiry)).toBeNull()
  }
)
