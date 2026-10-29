import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import api from '../utils/api'
import { useBotsStore } from './bots'

vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
it('forwards cancellation for scoped logs while preserving existing two-argument calls', async () => {
  const rows = [{ id: 1, message: 'fictional record' }]
  api.get.mockResolvedValue({ data: rows })
  const store = useBotsStore()
  const signal = new AbortController().signal
  expect(await store.getBotLogs(1, { level: 'ERROR' }, { signal })).toEqual(rows)
  expect(api.get).toHaveBeenLastCalledWith('/bots/1/logs', { params: { level: 'ERROR' }, signal })
  expect(await store.getBotLogs(2, { category: 'system' })).toEqual(rows)
  expect(api.get).toHaveBeenLastCalledWith('/bots/2/logs', { params: { category: 'system' } })
})

it('keeps detail reads as pure scoped results and forwards optional signals', async () => {
  const store = useBotsStore()
  const list = [{ id: 99, name: 'fictional list entry' }]
  store.bots = list
  const pending = []
  api.get.mockImplementation(
    (url, options) => new Promise((resolve) => pending.push({ url, options, resolve }))
  )
  const signal = new AbortController().signal
  const first = store.getBot(1, { signal })
  const second = store.getBot(2)
  expect(pending[0]).toMatchObject({ url: '/bots/1', options: { signal } })
  pending[1].resolve({ data: { id: 2 } })
  pending[0].resolve({ data: { id: 1 } })
  expect(await second).toEqual({ id: 2 })
  expect(await first).toEqual({ id: 1 })
  expect(store.bots).toEqual(list)
  const state = store.getBotState(2, { include_usage: true }, { signal })
  expect(pending[2]).toMatchObject({
    url: '/bots/2/state',
    options: { params: { include_usage: true }, signal },
  })
  pending[2].resolve({ data: { state: { n1: {} } } })
  expect(await state).toEqual({ state: { n1: {} } })
  const legacy = store.getBotState(1, { include_usage: false })
  expect(pending[3].options).toEqual({ params: { include_usage: false } })
  pending[3].resolve({ data: {} })
  await legacy
})
