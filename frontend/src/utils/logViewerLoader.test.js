import { describe, expect, it, vi } from 'vitest'
import { createLogViewerLoader } from './logViewerLoader'

const scope = (botId = 1, category = '', level = '') => ({ botId, category, level })
const row = (id) => ({ id, message: `fictional ${id}`, created_at: `2026-01-01T00:00:${id}Z` })
function fixture() {
  const pending = []
  const state = {}
  const publish = vi.fn((value) => Object.assign(state, value))
  const save = vi.fn()
  const request = vi.fn(
    (botId, params, signal) =>
      new Promise((resolve, reject) => pending.push({ botId, params, signal, resolve, reject }))
  )
  return {
    pending,
    state,
    publish,
    save,
    request,
    loader: createLogViewerLoader({ request, publish, save }),
  }
}

async function loaded(f, current = scope(), rows = [row(1)]) {
  const promise = f.loader.setScope(current)
  f.pending.at(-1).resolve(rows)
  await promise
}

describe('legacy log request scope', () => {
  it('clears an old bot immediately and ignores its pending response after the new bot fails', async () => {
    const f = fixture()
    const first = f.loader.setScope(scope(1))
    const second = f.loader.setScope(scope(2))
    expect(f.pending[0].signal.aborted).toBe(true)
    expect(f.state.logs).toEqual([])
    f.pending[1].reject(new Error('offline'))
    await second
    f.pending[0].resolve([row(1)])
    await first
    expect(f.state.logs).toEqual([])
    expect(f.state.loading).toBe(false)
  })

  it('fences a late refresh after category/level change and clears the previous result', async () => {
    const f = fixture()
    await loaded(f)
    const refresh = f.loader.refresh()
    expect(f.pending[1].params.since).toBe(row(1).created_at)
    const filtered = f.loader.setScope(scope(1, 'command', 'ERROR'))
    expect(f.state.logs).toEqual([])
    expect(f.pending[1].signal.aborted).toBe(true)
    expect(f.pending[2].params).toMatchObject({ category: 'command', level: 'ERROR', page: 1 })
    f.pending[1].resolve([row(2)])
    await refresh
    expect(f.state.logs).toEqual([])
    expect(f.state.loading).toBe(true)
    f.pending[2].resolve([row(3)])
    await filtered
    expect(f.state.logs.map((item) => item.id)).toEqual([3])
  })

  it('deduplicates polling and manual refresh in a scope and merges only current results', async () => {
    const f = fixture()
    await loaded(f)
    const poll = f.loader.refresh()
    expect(await f.loader.refresh()).toBe(false)
    expect(f.request).toHaveBeenCalledTimes(2)
    f.pending[1].resolve([row(2), row(1)])
    await poll
    expect(f.state.logs.map((item) => item.id)).toEqual([2, 1])
  })

  it('does not advance a failed load-more page or append a late page to a new bot', async () => {
    const f = fixture()
    await loaded(
      f,
      scope(),
      Array.from({ length: 50 }, (_, index) => row(index))
    )
    const failed = f.loader.loadMore()
    expect(f.pending[1].params.page).toBe(2)
    f.pending[1].reject(new Error('offline'))
    await failed
    const retry = f.loader.loadMore()
    expect(f.pending[2].params.page).toBe(2)
    const changed = f.loader.setScope(scope(2))
    f.pending[2].resolve([row(70)])
    await retry
    expect(f.state.logs).toEqual([])
    f.pending[3].resolve([row(80)])
    await changed
    expect(f.state.logs.map((item) => item.id)).toEqual([80])
  })

  it('does not fetch legacy rows in Agent history or revive them when an old request finishes', async () => {
    const f = fixture()
    const first = f.loader.setScope(scope())
    expect(await f.loader.setScope(scope(1, 'agent'))).toBe(false)
    expect(await f.loader.refresh()).toBe(false)
    expect(await f.loader.download()).toBe(false)
    expect(f.request).toHaveBeenCalledTimes(1)
    f.pending[0].resolve([row(1)])
    await first
    expect(f.state.logs).toEqual([])
  })

  it('captures the download filename and filters from the requested scope', async () => {
    const f = fixture()
    await loaded(f, scope(7, 'command', 'WARNING'))
    const download = f.loader.download()
    expect(await f.loader.download()).toBe(false)
    expect(f.pending[1]).toMatchObject({
      botId: 7,
      params: { category: 'command', level: 'WARNING', page: 1, limit: 10000 },
    })
    f.pending[1].resolve([row(1)])
    await download
    expect(f.save).toHaveBeenCalledWith({ rows: [row(1)], filename: 'bot_7_command_logs.txt' })
  })

  it.each([scope(2), scope(1, 'system'), scope(1, '', 'ERROR')])(
    'cancels downloads when the bot or either filter changes: %j',
    async (changedScope) => {
      const f = fixture()
      await loaded(f)
      const download = f.loader.download()
      const changed = f.loader.setScope(changedScope)
      expect(f.pending[1].signal.aborted).toBe(true)
      f.pending[1].resolve([row(1)])
      await download
      expect(f.save).not.toHaveBeenCalled()
      expect(f.state.downloading).toBe(false)
      f.pending[2].resolve([row(2)])
      await changed
    }
  )

  it('ignores late read/download results and starts no work after unmount', async () => {
    const f = fixture()
    await loaded(f)
    const refresh = f.loader.refresh()
    const download = f.loader.download()
    f.loader.dispose()
    expect(f.pending[1].signal.aborted).toBe(true)
    expect(f.pending[2].signal.aborted).toBe(true)
    const count = f.publish.mock.calls.length
    f.pending[1].resolve([row(2)])
    f.pending[2].resolve([row(1)])
    await Promise.all([refresh, download])
    expect(f.publish).toHaveBeenCalledTimes(count)
    expect(f.save).not.toHaveBeenCalled()
    expect(await f.loader.setScope(scope(2))).toBe(false)
    expect(await f.loader.refresh()).toBe(false)
    expect(await f.loader.download()).toBe(false)
    expect(f.request).toHaveBeenCalledTimes(3)
  })

  it('does not trigger a failed download and permits a scoped retry', async () => {
    const f = fixture()
    await loaded(f)
    const failed = f.loader.download()
    f.pending[1].reject(new Error('offline'))
    expect(await failed).toBe(false)
    expect(f.save).not.toHaveBeenCalled()
    expect(f.state.downloading).toBe(false)
    const retry = f.loader.download()
    f.pending[2].resolve([row(1)])
    await retry
    expect(f.save.mock.calls[0][0].filename).toBe('bot_1_all_logs.txt')
  })
})

it('clears private logs and cancels a private download when consent is closed', async () => {
  const f = fixture()
  await loaded(f, { ...scope(), includePrivate: true }, [
    { ...row(1), message: 'fictional-private' },
  ])
  const download = f.loader.download()
  expect(f.pending[1].params.include_private).toBe(true)
  const publicRead = f.loader.setScope({ ...scope(), includePrivate: false })
  expect(f.state.logs).toEqual([])
  expect(f.pending[1].signal.aborted).toBe(true)
  expect(f.pending[2].params.include_private).toBe(false)
  f.pending[1].resolve([{ ...row(2), message: 'fictional-private-export' }])
  await download
  expect(f.save).not.toHaveBeenCalled()
  f.pending[2].resolve([row(3)])
  await publicRead
  expect(f.state.logs).toEqual([row(3)])
})
