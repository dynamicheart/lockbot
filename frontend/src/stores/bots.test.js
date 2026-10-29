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
