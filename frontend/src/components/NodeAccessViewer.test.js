import { expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import NodeAccessViewer from './NodeAccessViewer.vue'
import api from '../utils/api'

vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))

it('loads saved addresses only when open and drops late responses after closing', async () => {
  const renderer = createRenderer({
    createElement: (tag) => ({ tag }),
    createText: (text) => ({ text }),
    createComment: (text) => ({ text }),
    insert: (node, parent) => {
      node.parent = parent
    },
    remove: () => {},
    setText: () => {},
    setElementText: () => {},
    parentNode: (node) => node.parent,
    nextSibling: () => null,
    patchProp: () => {},
  })
  const app = renderer.createApp(NodeAccessViewer, { botId: 15 })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.mount({})
  try {
    const state = app._instance.setupState
    expect(api.get).not.toHaveBeenCalled()
    let finish
    api.get.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve
        })
    )
    state.visible = true
    await nextTick()
    expect(api.get).toHaveBeenCalledWith('/bots/15/node-access', {
      signal: expect.any(AbortSignal),
      _silent: true,
    })
    const signal = api.get.mock.calls[0][1].signal
    state.visible = false
    await nextTick()
    expect(signal.aborted).toBe(true)
    const node = { host: 'compute-01.example.test', ipv4: '192.0.2.41', port: 22 }
    finish({ data: { nodes: { 'node-01': node } } })
    await Promise.resolve()
    expect(state.addresses).toBeNull()

    api.get.mockResolvedValueOnce({ data: { nodes: { 'node-01': node } } })
    state.visible = true
    await nextTick()
    await Promise.resolve()
    expect(state.addresses).toEqual([['node-01', node]])
  } finally {
    app.unmount()
  }
})
