import { describe, expect, it } from 'vitest'
import { isIdleResource, resourceStatus } from './resourceAvailability'

describe('resource availability during queue handoff', () => {
  it('keeps a released resource reserved until the queue is claimed or cancelled', () => {
    const resource = { lock_status: 'exclusive', booking_list: [{ user_id: 'bob' }] }
    expect(isIdleResource(resource)).toBe(false)
    resource.lock_status = 'idle'
    expect(resourceStatus(resource)).toBe('pending')
    expect(isIdleResource(resource)).toBe(false)
    resource.booking_list = []
    expect(isIdleResource(resource)).toBe(true)
  })
  it('does not turn an elapsed lease into availability before the server releases it', () => {
    expect(resourceStatus({ lock_status: 'exclusive', holders: [{ remaining_seconds: 0 }] })).toBe(
      'exclusive'
    )
  })
  it('supports non-queue resources and unknown status', () => {
    expect(isIdleResource({ lock_status: 'idle' })).toBe(true)
    expect(resourceStatus({ lock_status: 'shared' })).toBe('shared')
    expect(isIdleResource({})).toBe(false)
  })
})
