import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import SiteBroadcast from './SiteBroadcast.vue'
import api from '../utils/api'
import { ElMessageBox } from 'element-plus'
vi.mock('../utils/helpers', () => ({ useHelpers: () => ({ copyText: vi.fn() }) }))
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('element-plus', () => ({ ElMessageBox: { confirm: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
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
it('uses only supplied cached names and preserves preview-confirm-send without lookup requests', async () => {
  api.get.mockResolvedValue({ data: [] })
  const draft = {
    id: 'fixture-job',
    message: 'fictional notice',
    status: 'draft',
    total: 1,
    mention_all: false,
    counts: { pending: 1 },
    targets: [{ group_id: 'fixture-group' }],
  }
  api.post.mockImplementation(async (path) => ({
    data: path.endsWith('/preview') ? draft : { ...draft, status: 'queued' },
  }))
  app = renderer.createApp(SiteBroadcast)
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.mount({ children: [] })
  const view = app._instance.setupState
  await flush()
  expect(view.groupLabel({ group_name: 'known group', group_id: 'stable-id' })).toBe('known group')
  expect(view.groupLabel({ cached_group_name: 'cached group', group_id: 'stable-id' })).toBe(
    'cached group'
  )
  expect(view.groupLabel({ group_id: 'stable-id' })).toBe('stable-id')
  view.message = 'fictional notice'
  await view.preview()
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(api.post).toHaveBeenLastCalledWith('/admin/broadcasts/preview', {
    message: 'fictional notice',
    recent_days: 7,
    mention_all: false,
  })
  ElMessageBox.confirm.mockRejectedValueOnce(new Error('cancel'))
  await view.send()
  expect(api.post).toHaveBeenCalledTimes(1)
  ElMessageBox.confirm.mockResolvedValueOnce(true)
  await view.send()
  expect(api.post).toHaveBeenCalledTimes(2)
  expect(api.post).toHaveBeenLastCalledWith('/admin/broadcasts/fixture-job/send')
  expect(api.get.mock.calls.every(([path]) => path === '/admin/broadcasts')).toBe(true)
})
