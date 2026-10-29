import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ref } from 'vue'
import api from './api'
import { useFeishuDisplayNames } from './useFeishuDisplayNames'

vi.mock('./api', () => ({ default: { post: vi.fn(), get: vi.fn() } }))
const renderer = createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert: () => {},
  remove: () => {},
  setText: () => {},
  setElementText: () => {},
  parentNode: () => null,
  nextSibling: () => null,
  patchProp: () => {},
})
let app
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
}
function mount() {
  const scope = ref({ id: 1, key: 'app-a/bot-1/user-1' })
  let view
  app = renderer.createApp({
    setup() {
      view = useFeishuDisplayNames(scope)
      return () => h('div')
    },
  })
  app.mount({})
  return { scope, view }
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
})

it('clears immediately on A/B/A changes and rejects old responses and unmount completions', async () => {
  const requests = []
  api.post.mockImplementation(
    (path, body, options) => new Promise((resolve) => requests.push({ path, body, options, resolve }))
  )
  const { scope, view } = mount()
  expect(requests[0].body).toEqual({})
  scope.value = { id: 2, key: 'app-b/bot-2/user-1' }
  expect(view.names.value).toEqual({})
  expect(requests[0].options.signal.aborted).toBe(true)
  requests[1].resolve({ data: { names: { ou_b: 'Second user' }, status: 'ready' } })
  await flush()
  requests[0].resolve({ data: { names: { ou_a: 'Old user' }, status: 'ready' } })
  await flush()
  expect(view.label('ou_b')).toBe('Second user')
  expect(view.label('ou_a')).toBe('ou_a')
  scope.value = { id: 1, key: 'app-a/bot-1/user-1' }
  expect(view.names.value).toEqual({})
  app.unmount()
  app = null
  requests[2].resolve({ data: { names: { ou_a: 'Late user' }, status: 'ready' } })
  await flush()
  expect(view.names.value).toEqual({})
})

it('posts one batch then reads status with a finite polling budget', async () => {
  api.post.mockResolvedValue({ data: { status: 'pending', names: {} } })
  api.get.mockResolvedValue({ data: { status: 'pending', names: {} } })
  const { view } = mount()
  await flush()
  await vi.advanceTimersByTimeAsync(14000)
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(api.get).toHaveBeenCalledTimes(3)
  expect(view.status.value).toBe('unavailable')
  await vi.advanceTimersByTimeAsync(120000)
  expect(api.get).toHaveBeenCalledTimes(3)
})

it('keeps valid display-only names and stops polling on success', async () => {
  api.post.mockResolvedValue({ data: { status: 'pending', names: { ou_a: 'First' } } })
  api.get.mockResolvedValue({
    data: { status: 'ready', names: { ou_b: 'Second', arbitrary: 'ignored', ou_long: 'x'.repeat(81) } },
  })
  const { view } = mount()
  await flush()
  await vi.advanceTimersByTimeAsync(2000)
  expect(view.names.value).toEqual({ ou_a: 'First', ou_b: 'Second' })
  expect(view.status.value).toBe('ready')
  await vi.advanceTimersByTimeAsync(120000)
  expect(api.get).toHaveBeenCalledTimes(1)
})

it('clears labels on an authority error and never queries a non-Feishu scope', async () => {
  api.post.mockResolvedValueOnce({ data: { status: 'ready', names: { ou_a: 'First' } } })
  const { scope, view } = mount()
  await flush()
  api.post.mockRejectedValueOnce({ response: { status: 409 } })
  await view.refresh()
  expect(view.names.value).toEqual({})
  expect(view.status.value).toBe('unavailable')
  scope.value = null
  await view.refresh()
  expect(api.post).toHaveBeenCalledTimes(2)
})
