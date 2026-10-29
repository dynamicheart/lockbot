import { describe, expect, it, vi } from 'vitest'
import { createServiceTrafficLoader } from './serviceTrafficLoader'

function fixture() {
  const pending = []
  const state = {}
  const publish = vi.fn((value) => Object.assign(state, value))
  const request = vi.fn(
    (scope, signal) =>
      new Promise((resolve, reject) => pending.push({ scope, signal, resolve, reject }))
  )
  return {
    pending,
    state,
    publish,
    request,
    loader: createServiceTrafficLoader({ request, publish }),
  }
}

describe('service traffic scope lifecycle', () => {
  it('clears the previous bot immediately and keeps it absent when the new request fails', async () => {
    const f = fixture()
    const first = f.loader.setScope({ botId: null, filterBot: 1 })
    f.pending[0].resolve({ bot_id: 1, credentials: [{ id: 'fictional-a' }] })
    await first
    expect(f.state.snapshot.bot_id).toBe(1)
    const second = f.loader.setScope({ botId: null, filterBot: 2 })
    expect(f.state.snapshot).toBe(null)
    expect(f.state.loading).toBe(true)
    f.pending[1].reject(new Error('offline'))
    await second
    expect(f.state).toMatchObject({ snapshot: null, failed: true, loading: false })
  })

  it('ignores a deferred old-scope result even when transport cancellation is not honored', async () => {
    const f = fixture()
    const first = f.loader.setScope({ botId: null, filterBot: 1 })
    const second = f.loader.setScope({ botId: null, filterBot: 2 })
    expect(f.pending[0].signal.aborted).toBe(true)
    f.pending[0].resolve({ bot_id: 1 })
    await first
    expect(f.state.snapshot).toBe(null)
    expect(f.state.loading).toBe(true)
    f.pending[1].resolve({ bot_id: 2 })
    await second
    expect(f.state.snapshot.bot_id).toBe(2)
  })

  it('deduplicates manual refresh and polling within the same scope', async () => {
    const f = fixture()
    const first = f.loader.setScope({ botId: 1 })
    expect(await f.loader.refresh()).toBe(false)
    expect(await f.loader.setScope({ botId: 1 })).toBe(false)
    expect(f.request).toHaveBeenCalledTimes(1)
    f.pending[0].resolve({ bot_id: 1 })
    await first
    const poll = f.loader.refresh()
    expect(f.request).toHaveBeenCalledTimes(2)
    f.pending[1].resolve({ bot_id: 1 })
    await poll
  })

  it('does not publish after disposal and waits for visibility before requesting a new scope', async () => {
    let visible = false
    const request = vi.fn(async () => ({ bot_id: 2 }))
    const publish = vi.fn()
    const loader = createServiceTrafficLoader({ request, publish, canRequest: () => visible })
    await loader.setScope({ botId: 2 })
    expect(publish).toHaveBeenCalledWith({ snapshot: null, failed: false, loading: false })
    expect(request).not.toHaveBeenCalled()
    visible = true
    await loader.refresh()
    expect(request).toHaveBeenCalledTimes(1)
    const f = fixture()
    const pending = f.loader.setScope({ botId: 1 })
    f.loader.dispose()
    expect(f.pending[0].signal.aborted).toBe(true)
    const count = f.publish.mock.calls.length
    f.pending[0].resolve({ bot_id: 1 })
    await pending
    expect(f.publish).toHaveBeenCalledTimes(count)
  })
})

it('treats private consent as part of the scope and fences late credential snapshots', async () => {
  const f = fixture()
  const privateRead = f.loader.setScope({ botId: 1, includePrivate: true })
  const publicRead = f.loader.setScope({ botId: 1, includePrivate: false })
  expect(f.state.snapshot).toBe(null)
  expect(f.pending[0].signal.aborted).toBe(true)
  f.pending[0].resolve({ credentials: [{ id: 'fictional-private' }] })
  await privateRead
  expect(f.state.snapshot).toBe(null)
  f.pending[1].resolve({ credentials: [{ user: 'alice' }] })
  await publicRead
  expect(f.state.snapshot.credentials).toEqual([{ user: 'alice' }])
})
