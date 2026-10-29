import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import FeishuGroupInfo from './FeishuGroupInfo.vue'
import api from '../utils/api'
vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))
vi.mock('../utils/helpers', () => ({ useHelpers: () => ({ copyText: vi.fn() }) }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent, anchor) => {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove: (child) => {
    const list = child.parent?.children
    const index = list?.indexOf(child)
    if (index >= 0) list.splice(index, 1)
  },
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: () => {},
})
let app
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
}
afterEach(() => {
  app?.unmount()
  app = null
  vi.clearAllMocks()
})
it('isolates late names and aborts lookup on scope change and unmount', async () => {
  const requests = []
  api.get.mockImplementation(
    (path, options) =>
      new Promise((resolve, reject) => requests.push({ path, options, resolve, reject }))
  )
  app = renderer.createApp(FeishuGroupInfo, { botId: 1, chatId: 'oc_fixture_one' })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.mount({ children: [] })
  const state = app._instance.setupState
  expect(requests[0].path).toBe('/feishu/bots/1/group')
  app._instance.props.botId = 2
  app._instance.props.chatId = 'oc_fixture_two'
  await flush()
  expect(requests[0].options.signal.aborted).toBe(true)
  const current = requests.at(-1)
  expect(current.path).toBe('/feishu/bots/2/group')
  current.resolve({ data: { name: 'second group', status: 'ok' } })
  await flush()
  requests[0].resolve({ data: { name: 'old group', status: 'ok' } })
  await flush()
  expect(state.group.name).toBe('second group')
  state.load(true)
  const pending = requests.at(-1)
  app.unmount()
  app = null
  expect(pending.options.signal.aborted).toBe(true)
  pending.reject({ code: 'ERR_CANCELED' })
  await flush()
  expect(state.group.status).toBe('ok')
})
